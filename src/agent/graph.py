"""LangGraph 状态图构建 — Agent ↔ Tools 循环，含 RPA 按需注入 + Flash 意图路由。

每次对话开始时用 Flash 模型做轻量意图分类 + 工具评分，按意图路由到
不同处理通道（直接回答 / RAG 检索 / 联网搜索 / 全工具），避免简单问题
也走完整 RAG 搜索和全工具绑定的开销。
"""

import asyncio
import logging
from datetime import datetime
from typing import Literal

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

from .state import AgentState, SpecialistState
from .core import (
    create_llm,
    create_llm_with_fallback,
    WEB_SEARCH_TOOL,
    get_system_prompt,
    get_all_tools,
    get_core_tools,
    set_session_prompt_tokens,
)
from .specialists import (
    SUPERVISOR_PROMPT,
    SPECIALISTS,
    GENERAL,
    match_specialist_tools,
    get_specialist,
)
from .utils import extract_user_text, content_to_text, messages_have_image, is_tool_error, extract_first_json
from .context_budget import messages_tokens
from .approval import classify_tool_risk, build_approval_payload, get_approval_mode, check_command_policy
from config import (
    LLM_MODEL,
    LLM_FLASH_MODEL,
    WEB_SEARCH_ENABLED,
    SECURITY_LLM_GUARD,
    RAG_TOP_K,
    RAG_MODE,
    display_model_name,
)
from rag import async_search_knowledge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 基础设施：Checkpoint + Observability
# ---------------------------------------------------------------------------


async def _create_checkpointer():
    """创建 SQLite checkpoint saver，持久化对话历史。

    优先使用 SQLite（兼容所有平台和事件循环），
    PostgreSQL 作为可选替代（需要 psycopg + SelectorEventLoop，Windows 有兼容问题）。
    """
    import os as _os
    from pathlib import Path

    # ── 优先 SQLite（零配置、跨平台、完美持久化）──
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        data_dir = Path(__file__).resolve().parent.parent / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(data_dir / "checkpoint.db")

        # from_conn_string 返回 async context manager，需要 async with 获取 saver
        # 直接使用构造函数创建并手动 setup
        import aiosqlite
        conn = await aiosqlite.connect(db_path)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        logger.info("SQLite checkpoint store ready: %s", db_path)
        return saver
    except Exception as exc:
        logger.warning("SQLite checkpoint unavailable (%s), falling back to MemorySaver", exc)

    return MemorySaver()

# ---------------------------------------------------------------------------
# 工具错误检测 — 统一在 agent/utils.py 中定义，这里做别名保持兼容
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 纯函数（模块级，便于测试）
# ---------------------------------------------------------------------------


def _should_continue(state: AgentState) -> Literal["tools", "reflect", "__end__"]:
    """判断工具调用后的路由：继续 / 反思 / 结束。"""
    last_msg = state["messages"][-1]
    failures = state.get("tool_failures", 0)
    retries = state.get("tool_retries", 0)
    if not (hasattr(last_msg, "tool_calls") and last_msg.tool_calls):
        return "__end__"
    if failures >= 3:
        return "__end__"
    if failures > 0 and retries < 1:
        return "reflect"
    return "tools"


# ── RPA 工具前缀（用于历史检测和模型路由）──
# 含 submit_rpa_：提交任务后跨轮保留 RPA 工具绑定（用户接着问任务进度）。
_RPA_TOOL_PREFIXES = ("mcp_rpa_", "rpa_", "amazon_", "mcp_docker_", "submit_rpa_")


def _sanitize_messages_for_api(messages: list) -> list:
    """清理消息历史中不完整的 tool_call 序列，避免 DeepSeek 400 错误。

    DeepSeek（以及其他严格校验的 LLM 提供商）要求：
    每条带有 tool_calls 的 assistant 消息之后，必须有对应的 tool 消息
    响应每一个 tool_call_id。如果 tool 调用后被 reflect 节点中断，
    会留下没有对应 ToolMessage 的 tool_calls AIMessage，
    导致下一次 LLM 调用时 API 返回 400。

    此函数移除所有孤立的（无匹配 ToolMessage 的）tool_calls AIMessage，
    同时保留 LLM 的文本回复内容。
    """
    from langchain_core.messages import ToolMessage

    cleaned: list = []

    # 第一遍：扫描所有消息，建立 tool_call_id 索引
    all_tool_msg_ids: set[str] = set()
    for m in messages:
        if isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", "") or ""
            if tc_id:
                all_tool_msg_ids.add(tc_id)

    # 第二遍：过滤消息
    surviving_tool_ids: set[str] = set()  # 保留下来、仍被 ToolMessage 引用的 AI tool_call id
    for m in messages:
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            tool_ids: set[str] = set()
            for tc in m.tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id:
                    tool_ids.add(tc_id)

            if tool_ids:
                # 检查是否有足够的 ToolMessage 覆盖所有 tool_call_ids
                if tool_ids.issubset(all_tool_msg_ids):
                    cleaned.append(m)
                    surviving_tool_ids.update(tool_ids)
                else:
                    # 孤立的 tool_calls：作为纯文本消息保留（如果有 content）
                    missing = tool_ids - all_tool_msg_ids
                    logger.warning(
                        "Dropping orphaned tool_calls (missing ToolMessages for: %s)", missing,
                    )
                    if getattr(m, "content", None):
                        cleaned.append(type(m)(content=m.content))
                continue

        # 反向防御：孤立的 ToolMessage —— 其对应 AI tool_call 已被摘要压缩/上面清理掉，
        # 继续保留会触发 DeepSeek 400 "Messages with role 'tool' must be a response to
        # a preceding message with 'tool_calls'"，整轮 LLM 调用失败。
        if isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", "") or ""
            if tc_id and tc_id not in surviving_tool_ids:
                logger.warning(
                    "Dropping orphaned ToolMessage (no surviving tool_call for: %s)", tc_id,
                )
                continue

        cleaned.append(m)

    return cleaned


async def _replace_images_with_descriptions(messages: list, question: str = "") -> list:
    """把多模态消息里的图片块替换为视觉模型的文字描述。

    主模型（deepseek-v4-flash/pro）是纯文本模型，直接收到 image_url 块会
    返回 400（"This model does not support image"），且回退链上也都是文本
    模型，最终整轮报错。因此在喂给主模型前，先把图片交给视觉模型
    （deepseek-v4-flash-vision-exp）转成文字描述，主模型基于描述推理。

    该函数对已转换的消息幂等（转换后无 image_url 块，直接原样返回），
    因此同一图片在后续轮次不会重复触发视觉调用……除首次外的每轮仍会
    从 checkpoint 载入原始 image_url 块并重新转换（视觉调用会按轮次重复）。
    视觉调用失败/未启用时，图片块降级为占位文本，绝不让主链路因图片报错。

    Args:
        messages: 原始消息列表（含可能的 image_url 块）。
        question: 用户本轮问题文本，作为视觉模型聚焦描述的引导。

    Returns:
        转换后的新消息列表（原列表不被修改）。
    """
    from .vision import describe_image, DEFAULT_DESCRIBE_PROMPT

    result: list = []
    for m in messages:
        content = getattr(m, "content", None)
        if not isinstance(content, list):
            result.append(m)
            continue

        image_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "image_url"]
        if not image_blocks:
            result.append(m)
            continue

        # 该消息里与图片并列的纯文本块（通常是用户的问题/说明）
        local_text = " ".join(
            str(b.get("text", "")) for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ).strip()
        guiding = (local_text or question).strip()

        new_content: list = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                uri = block.get("image_url", {}).get("url", "")
                if guiding:
                    prompt = f"{guiding}\n{DEFAULT_DESCRIBE_PROMPT}"
                else:
                    prompt = DEFAULT_DESCRIBE_PROMPT
                desc = await describe_image(uri, prompt=prompt)
                if desc:
                    new_content.append({
                        "type": "text",
                        "text": (
                            "[图片内容已识别，请直接据此回答，无需再调用图片理解工具]"
                            f"\n{desc}"
                        ),
                    })
                else:
                    new_content.append({
                        "type": "text",
                        "text": "[图片内容未能识别（视觉模型不可用），请告知用户无法解析该图片]",
                    })
            else:
                new_content.append(block)

        # 用同类型消息重建，避免改变 LangChain 消息类型
        result.append(type(m)(content=new_content))
    return result


def _route_after_classify(state: AgentState) -> Literal["supervisor", "query_rewrite", "agent"]:
    """分类后路由：complex → supervisor（多 Agent 委派），需要 RAG → 先改写查询词再检索，其余 → agent。

    含图片（多模态）的消息直接进 agent：视觉描述已在 call_model 里由
    ``_replace_images_with_descriptions`` 注入上下文，无需 RAG 检索，
    也无需 supervisor/planner 规划（否则"描述这张图片"会被规划器劫持，
    输出步骤计划而非直接回答）。
    """
    if messages_have_image(state.get("messages", [])):
        return "agent"
    if state.get("needs_rag"):
        return "query_rewrite"
    intent = state.get("intent", "complex")
    # code 意图也进 supervisor → coder 子代理（否则代码任务永远由主代理直处理，
    # coder 子代理不可达）。2026-09-02 冒烟发现后修改。
    if intent in ("complex", "code"):
        return "supervisor"
    if intent == "trivial":
        return "agent"
    return "agent"


def _route_after_rag(state: AgentState) -> Literal["supervisor", "agent"]:
    """RAG 检索后路由：根据意图分发到对应节点。"""
    intent = state.get("intent", "complex")
    if messages_have_image(state.get("messages", [])):
        return "agent"
    if intent in ("complex", "code"):
        return "supervisor"
    return "agent"


# ── Plan-Execute-Replan 常量 ──
_MAX_REPLAN = 3  # 最多重规划 3 次


def _route_after_plan_check(state: AgentState) -> Literal["replan", "agent", "__end__"]:
    """计划检查后路由。

    - 没有计划 → 回到 agent 继续正常循环（由 _should_continue 处理终止）
    - 有失败步骤 → 触发重规划
    - 有 pending 步骤 → 继续 agent
    - 全部完成 → 回到 agent，让 LLM 基于最终工具结果生成总结

    注意：不再在任何情况下返回 __end__，循环终止由 _should_continue
    (agent 后路由) 统一处理——当 LLM 生成无 tool_calls 的最终回复时自然结束。
    """
    steps = state.get("plan_steps", [])
    if not steps:
        # 无计划 → 回到 agent 继续正常对话循环
        return "agent"

    any_failed = any(s.get("status") == "failed" for s in steps)
    replan_count = state.get("replan_count", 0)

    if any_failed and replan_count < _MAX_REPLAN:
        return "replan"
    # 全部完成或有 pending 步骤 → 回到 agent 继续处理
    return "agent"


def _route_after_replan(state: AgentState) -> Literal["agent", "__end__"]:
    """重规划后路由：有新步骤 → 继续 agent，无可执行步骤 → 结束。"""
    steps = state.get("plan_steps", [])
    has_pending = any(s.get("status") == "pending" for s in steps)
    if has_pending:
        return "agent"
    return "__end__"


# ---------------------------------------------------------------------------
# Human-in-the-Loop：审批路由
# ---------------------------------------------------------------------------


def _route_after_approval(state: AgentState) -> Literal["tools", "agent"]:
    """审批节点后路由：批准/低风险 → 执行工具，拒绝 → 回到 agent 解释原因。"""
    decision = state.get("approval_decision", "")
    if decision == "denied":
        return "agent"  # 跳过工具执行，让 LLM 告知用户
    # "approved" 或无需审批 → 正常执行工具
    return "tools"


# ---------------------------------------------------------------------------
# 真 Sub-Agent（LangGraph 子图）：supervisor 委派 → run_specialist 命令式执行子图
# ---------------------------------------------------------------------------


# 走子图执行的 specialist（general 保持单循环逻辑）
SUBAGENT_NAMES: set[str] = {"researcher", "coder", "analyst"}

# SessionCosts.to_dict() 的字段集合（graph.py call_model 成本记账使用）
_COST_KEYS = ("prompt_tokens", "completion_tokens", "cost_usd", "llm_calls", "latency_ms")


def _merge_session_costs(a: dict, b: dict) -> dict:
    """合并两份 session 成本明细（父 state 已累计 + 子代理内部累计）。"""
    return {k: (a.get(k, 0) or 0) + (b.get(k, 0) or 0) for k in _COST_KEYS}


def _extract_subagent_report(messages: list) -> str:
    """从子图最终 messages 提取子代理最终报告。

    从后向前找第一条无 tool_calls 且有内容的 AI 消息（子代理自然终止时
    最后一条即最终回答；达到迭代上限时可能是封顶提示）。
    """
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
            return content_to_text(m.content)
    return ""


def _route_after_supervisor(state: AgentState) -> Literal["run_specialist", "planner", "agent"]:
    """Supervisor 后路由：真 specialist → 子图执行；general 有任务 → planner 规划；否则 agent。"""
    specialist = state.get("specialist", "")
    task = state.get("specialist_task", "")
    if specialist in SUBAGENT_NAMES and task:
        return "run_specialist"
    if specialist and task:
        return "planner"
    return "agent"


async def sub_check_approval(state: AgentState) -> dict:
    """子代理内审批门控：不 interrupt，高风险调用直接拒绝并引导标注待审批操作。

    与父图 check_approval_node 的区别：子图无 checkpointer，interrupt 无法持久化
    resume，因此对需审批的调用一律返回 denied ToolMessage，由主代理在子图返回后
    以 general 身份走现有审批链路执行（安全性不变，能力分两阶段：子代理产出报告 →
    主代理经用户批准后执行高风险操作）。
    """
    import json as _json
    from langchain_core.messages import ToolMessage

    last_msg = state["messages"][-1]
    calls = last_msg.tool_calls or []
    mode = get_approval_mode()
    risky: list[dict] = []
    for tc in calls:
        if isinstance(tc, dict):
            cid, cname, cargs = tc.get("id", ""), tc.get("name", ""), tc.get("args", {})
        else:
            cid, cname, cargs = tc.id, tc.name, tc.args
        needs, _, _ = classify_tool_risk(cname, mode)
        if needs:
            risky.append({"id": cid, "name": cname, "args": cargs})
    if not risky:
        return {"approval_decision": ""}

    denied = [
        ToolMessage(
            content=_json.dumps({
                "status": "denied",
                "message": "此操作需要人工审批，子 Agent 无法直接执行。"
                           "请在最终报告中明确列出该待审批操作（工具名与参数），由主 Agent 在用户批准后执行。",
            }, ensure_ascii=False),
            tool_call_id=c["id"], name=c["name"],
        ) for c in risky
    ]
    prev = list(state.get("denied_tool_calls", []))
    return {
        "messages": denied,
        "approval_decision": "denied",
        "denied_tool_calls": prev + risky,
    }


def build_specialist_subgraph(
    *,
    agent_node,
    tool_node,
    reflect_node,
    track_failures,
    check_approval_node=sub_check_approval,
) -> CompiledStateGraph:
    """构建 specialist 子图（无 checkpointer，命令式 ainvoke）。

    复用父图同一批节点闭包：agent(带迭代上限包装) / tools / reflect / track_failures /
    sub_check_approval。关键约束（已验证 langgraph 1.2.11 源码）：
    - 每个 add_node 显式 input_schema=SpecialistState（否则从闭包 `state: AgentState`
      注解推断 input_schema，编译期抛 ValueError）。
    - 条件边用 lambda 包一层，避免从 `AgentState` 注解推断分支 schema。
    - sub.compile(checkpointer=False) 强制禁用 checkpoint（默认 None 会继承父图
      saver，污染父 thread 的 checkpoint store）。
    """
    sub = StateGraph(SpecialistState)
    sub.add_node("agent", agent_node, input_schema=SpecialistState)
    sub.add_node("check_approval", check_approval_node, input_schema=SpecialistState)
    sub.add_node("tools", tool_node, input_schema=SpecialistState)
    sub.add_node("reflect", reflect_node, input_schema=SpecialistState)
    sub.add_node("track_failures", track_failures, input_schema=SpecialistState)
    sub.set_entry_point("agent")
    sub.add_conditional_edges(
        "agent", lambda s: _should_continue(s),
        {"tools": "check_approval", "reflect": "reflect", "__end__": END},
    )
    sub.add_conditional_edges(
        "check_approval", lambda s: _route_after_approval(s),
        {"tools": "tools", "agent": "agent"},
    )
    sub.add_edge("tools", "track_failures")
    sub.add_edge("track_failures", "agent")
    sub.add_edge("reflect", "agent")
    return sub.compile(checkpointer=False)


def _build_tool_catalog() -> str:
    """从已注册工具中提取 name + description 构建分类器参考目录。"""
    tools = get_all_tools()
    lines = []
    for t in tools:
        desc = (t.description or "").split("\n")[0][:100]
        lines.append(f"- {t.name}: {desc}")
    return "\n".join(lines)


INTENT_TOOL_GROUPS: dict[str, list[str]] = {
    "trivial": [],
    "knowledge": [
        "tool_search_knowledge",
        "tool_list_knowledge_sources",
        "tool_index_knowledge",
        "tool_add_memory",
        "tool_search_memory",
    ],
    "web": [
        "mcp_searxng_web_search",
        "mcp_web_url_read",
    ],
    "file": [
        "mcp_read_file", "mcp_write_file", "mcp_edit_file",
        "mcp_list_directory", "mcp_directory_tree", "mcp_search_files",
        "mcp_move_file", "mcp_get_file_info",
    ],
    "code": ["execute_code"],
    "rpa": [],   # RPA 工具通过 _classify_needs_rpa 独立判断
    "time": ["mcp_get_current_time"],
    "complex": [],  # 空 = 全部工具
}

# 所有非 trivial 意图都加上记忆工具
_MEMORY_TOOLS = ["tool_add_memory", "tool_search_memory", "tool_forget_memory", "tool_list_memories"]

CLASSIFIER_SYSTEM = """你是一个智能路由器。分析用户请求，输出意图类型和是否需要知识库检索。

## 意图类型
- trivial: 简单问候/告别/感谢/确认（不包含个人信息分享）
- knowledge: 查询内部文档/产品数据/已索引的资料
- web: 实时信息/新闻/天气/最新动态
- file: 读写文件/查看目录
- code: 执行代码/数据分析/生成图表
- rpa: 浏览器自动化/Amazon店铺操作/紫鸟
- time: 获取当前时间/日期/星期
- complex: 多步推理/多工具协同/复杂任务

## 判断规则（按优先级）
1. **用户分享个人信息（姓名/职位/公司/偏好/习惯）→ 不得归类为 trivial，应归为 knowledge（保存记忆）**
2. 简单问候（你好/再见/谢谢/好的/知道了）→ trivial
3. 涉及产品SKU/订单/库存/店铺数据等内部业务 → knowledge
4. 涉及新闻/天气/股价/最新资讯 → web
5. 涉及Amazon/紫鸟/店铺后台操作 → rpa
6. 其余按实际需求判断

## 输出格式
{"intent":"<类型>","tools":["tool_name"],"needs_rag":true|false}
只输出JSON，不要其他内容。"""

CLASSIFIER_USER = """## 可用工具
{tool_catalog}

## 用户请求
{user_query}"""


def _is_tool_denied(msg) -> bool:
    """判断一条 ToolMessage 是否表示用户拒绝了操作（非执行失败）。"""
    import json as _json
    if not hasattr(msg, "content"):
        return False
    content = msg.content
    if isinstance(content, dict):
        return content.get("status") == "denied"
    if isinstance(content, str):
        try:
            parsed = _json.loads(content)
            return isinstance(parsed, dict) and parsed.get("status") == "denied"
        except (_json.JSONDecodeError, TypeError):
            pass
    return False


def _track_failures(state: AgentState) -> dict:
    """扫描最新工具消息，更新失败/重试计数。

    注意：用户拒绝（status: denied）不视为工具执行失败。
    """
    for m in reversed(state["messages"]):
        if m.type == "tool" and _is_tool_denied(m):
            # 用户拒绝 → 重置失败计数（不是工具错误）
            return {"tool_failures": 0, "tool_retries": 0}
        if m.type == "tool" and is_tool_error(m):
            return {"tool_failures": state.get("tool_failures", 0) + 1}
        if m.type == "tool":
            return {"tool_failures": 0, "tool_retries": 0}
    return {"tool_failures": 0, "tool_retries": 0}


# ---------------------------------------------------------------------------
# Agent Graph 构建
# ---------------------------------------------------------------------------


async def build_agent(context_summary: str = "") -> CompiledStateGraph:
    """创建并编译 LangGraph 智能体图，含 RPA 按需注入。"""

    # ── LLM 实例（Flash/Pro 双模型架构 + 三级回退）──
    # Flash: 轻量快速，处理简单对话、意图分类、计划生成、反思
    # Pro:   深度推理，处理复杂任务和多工具调用
    # 每个模型都通过 create_llm_with_fallback 创建，主模型不可用时自动切换备用
    llm_flash = create_llm_with_fallback(model=LLM_FLASH_MODEL, temperature=0.3)
    llm_pro = create_llm_with_fallback(model=LLM_MODEL, temperature=0.3)
    llm_flash_zero = create_llm_with_fallback(model=LLM_FLASH_MODEL, temperature=0)

    # ── 运行时回退链备用模型（API 调用失败时按序切换，见 call_model）──
    from config import LLM_FALLBACK_MODEL, LLM_FALLBACK_MODEL_2
    _runtime_fallbacks: list[tuple[str, object]] = []
    _seen_models = {LLM_FLASH_MODEL, LLM_MODEL}
    for _fb_name in (LLM_FALLBACK_MODEL, LLM_FALLBACK_MODEL_2):
        if not _fb_name or _fb_name in _seen_models:
            continue
        _seen_models.add(_fb_name)
        try:
            _runtime_fallbacks.append((_fb_name, create_llm(model=_fb_name, temperature=0.3)))
        except Exception:
            logger.warning("运行时备用模型 %s 创建失败，跳过", _fb_name, exc_info=True)

    CLASSIFIER_PROMPT = (
        "你是一个意图分类器。判断用户请求是否需要「紫鸟浏览器自动化（RPA）工具」。\n"
        "RPA 工具用于：打开浏览器/店铺、网页导航、点击页面元素、填写表单、\n"
        "查询 Amazon 广告数据、提取网页内容、读写 Excel 广告报表。\n"
        "只回答 YES 或 NO，不要解释。"
    )

    # ── 工具集：核心 vs 全部，Flash vs Pro ──
    _web_search_tools = [WEB_SEARCH_TOOL] if WEB_SEARCH_ENABLED else []
    core_tools = get_core_tools() + _web_search_tools

    def _get_all_tools():
        """动态获取全部工具（支持 MCP 懒加载后自动出现在工具列表中）。"""
        return get_all_tools() + _web_search_tools

    def _get_tool_by_name():
        """动态构建 tool_name → tool_object 映射。跳过非 LangChain tool 对象（如 dict）。"""
        return {t.name: t for t in _get_all_tools() if hasattr(t, "name")}

    async def _classify_needs_rpa(user_text: str) -> bool:
        """用 Flash 判断当前请求是否需要浏览器自动化工具。

        注意：使用原生 OpenAI SDK 而非 LangChain ChatOpenAI，避免 LangGraph
        流式回调系统捕获分类器输出（"YES"/"NO"）并推送给前端。
        """
        try:
            from .client_factory import get_async_openai_client
            client = get_async_openai_client()
            resp = await client.chat.completions.create(
                model=LLM_FLASH_MODEL,
                messages=[
                    {"role": "system", "content": CLASSIFIER_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=0,
                max_tokens=5,
            )
            return "YES" in (resp.choices[0].message.content or "").upper()
        except Exception:
            logger.debug("RPA 意图分类失败，默认不使用 RPA 工具", exc_info=True)
            return False

    def _history_has_rpa_calls(messages: list) -> bool:
        """检查历史消息中是否已有 RPA 工具调用（说明当前正在执行 RPA 任务）。"""
        for m in messages:
            if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    if name.startswith(_RPA_TOOL_PREFIXES):
                        return True
        return False

    full_prompt = get_system_prompt() + f"\n\n当前日期: {datetime.now().strftime('%Y年%m月%d日')}"
    if context_summary:
        full_prompt += f"\n\n{context_summary}"

    MAX_ITERATIONS = 30  # Agent 单次对话的最大思考-行动轮数
    SUBAGENT_MAX_ITERATIONS = 10  # 子代理（specialist 子图）单次委派的最大思考-行动轮数
    _subgraph_active = {"on": False}  # 子图执行标记（可变容器，避免闭包 nonlocal 复杂化）

    async def call_model(state: AgentState) -> dict:
        # ── 迭代上限保护（防止无限循环）──
        iteration = state.get("iteration_count", 0)
        if iteration >= MAX_ITERATIONS:
            logger.warning("Agent 达到最大迭代次数 %d，强制终止", MAX_ITERATIONS)
            return {
                "messages": [AIMessage(content=(
                    "已达到本轮对话的处理上限（30轮思考-行动）。"
                    "为避免过度消耗，我已停止继续处理。请开启新对话或简化您的问题后重试。"
                ))],
            }

        messages = list(state["messages"])
        # 图片是否出现在原始消息里（转换前判定，供缓存/路由跳过，见下）
        _had_image = messages_have_image(messages)
        user_text = extract_user_text(messages)
        # ── 上下文预算：把本会话已消耗 prompt token 透传给 get_context_remaining 工具 ──
        _sc = state.get("session_costs") or {}
        if isinstance(_sc, dict) and _sc.get("prompt_tokens") is not None:
            set_session_prompt_tokens(int(_sc.get("prompt_tokens", 0)))
        else:
            set_session_prompt_tokens(messages_tokens(messages))
        # ── 图片 → 文字描述：主模型是纯文本模型，先交给视觉模型转写 ──
        if _had_image:
            messages = await _replace_images_with_descriptions(messages, question=user_text)
            # 转换后重取文本（占位图片已被替换为文字描述，但文本部分通常不变）
            user_text = extract_user_text(messages) or user_text
        intent = state.get("intent", "complex")
        selected_tools = state.get("selected_tools", [])
        needs_rag = state.get("needs_rag", False)

        # ── 安全：输入防护（prompt 注入 / 越狱检测）──
        if SECURITY_LLM_GUARD and user_text:
            from security.guard import get_input_guard
            try:
                guard = get_input_guard()
                guard_result = await guard.check(user_text)
                if not guard_result.passed:
                    logger.warning(
                        "InputGuard 拦截: severity=%s reason=%s",
                        guard_result.severity, guard_result.reason,
                    )
                    return {
                        "messages": [AIMessage(content=(
                            "抱歉，您的输入包含不安全的内容，已被安全防护系统拦截。\n"
                            f"原因：{guard_result.reason}\n"
                            "如需帮助，请重新表述您的问题。"
                        ))],
                    }
            except Exception:
                logger.debug("InputGuard check failed", exc_info=True)

        # ── 对话摘要：超过阈值时自动压缩早期消息 ──
        from .summarizer import get_summarizer
        summarizer = get_summarizer()
        summary = state.get("conversation_summary", "")
        extra_updates: dict = {}
        if summarizer.should_summarize(messages):
            # 压缩不包含 system message
            user_assistant_msgs = [m for m in messages if not isinstance(m, SystemMessage)]
            new_summary = await summarizer.summarize(user_assistant_msgs, existing_summary=summary)
            if new_summary and new_summary != summary:
                summary = new_summary
                extra_updates["conversation_summary"] = summary
                logger.debug("Conversation summarized: %d chars", len(summary))

        # ── RPA 工具按需注入 ──
        if _subgraph_active["on"]:
            # 子代理不注入 RPA 工具：RPA 属高风险、需人工审批，由主代理
            # general 在子图返回后经现有审批链路执行。跳过逐轮 Flash 分类，省成本。
            need_rpa = False
        elif intent == "rpa" or _history_has_rpa_calls(messages):
            need_rpa = True
        elif user_text:
            need_rpa = await _classify_needs_rpa(user_text)
        else:
            need_rpa = False

        # ── Flash/Pro 模型路由 ──
        # 该 agent 以知识问答/联网搜索为主，Flash 已足以覆盖绝大多数请求，
        # 默认约 90% 走 Flash。Pro 仅在以下少数场景启用（阈值可用环境变量调整）：
        #   1. RPA 浏览器自动化任务（高风险操作，需要更强的规划与安全判断）
        #   2. complex 意图且输入超长（>PRO_ROUTE_MIN_CHARS 字）或会话已很深
        #      （>PRO_ROUTE_MIN_MESSAGES 条消息）
        from config import PRO_ROUTE_MIN_CHARS, PRO_ROUTE_MIN_MESSAGES
        if need_rpa:
            model_key = "pro"
            route_reason = "RPA task"
        elif (
            intent == "complex"
            and (len(user_text) > PRO_ROUTE_MIN_CHARS or len(messages) > PRO_ROUTE_MIN_MESSAGES)
        ):
            model_key = "pro"
            route_reason = "complex query"
        else:
            model_key = "flash"
            route_reason = "simple query"

        logger.info(
            "路由决策: need_rpa=%s intent=%s user_len=%d msg_count=%d → %s (%s)",
            need_rpa, intent, len(user_text), len(messages), model_key, route_reason,
        )

        # ── 工具绑定：按 selected_tools 筛选 ──
        # 核心原则：记忆工具始终可用，确保用户在任何对话中分享的个人信息都能被保存
        _memory_tool_names = {"tool_add_memory", "tool_search_memory",
                              "tool_forget_memory", "tool_list_memories"}
        _all_tools_map = _get_tool_by_name()
        _memory_tools = [t for name, t in _all_tools_map.items() if name in _memory_tool_names]

        # ── 多 Agent 协作：specialist 工具筛选 + 提示词后缀 ──
        specialist_name = state.get("specialist", "")
        specialist_suffix = ""
        _specialist_tools: list = []
        if specialist_name and specialist_name != "general":
            sp = get_specialist(specialist_name)
            if sp:
                _specialist_tools = match_specialist_tools(sp, _all_tools_map)
                specialist_suffix = sp.system_prompt_suffix
                logger.debug(
                    "Specialist '%s': %d tools matched — %s",
                    specialist_name, len(_specialist_tools), sp.display_name,
                )

        if intent == "trivial":
            # trivial 意图也至少绑定记忆工具，确保用户个人信息能被保存
            tools_to_bind = list(_memory_tools)
        elif intent == "complex" or need_rpa:
            if specialist_name and specialist_name != "general" and _specialist_tools:
                tools_to_bind = list(_specialist_tools)
                # 仍然追加记忆工具
                for mt in _memory_tools:
                    if mt not in tools_to_bind:
                        tools_to_bind.append(mt)
            else:
                tools_to_bind = _get_all_tools()
        elif selected_tools:
            tools_to_bind = [_all_tools_map[n] for n in selected_tools if n in _all_tools_map]
            # 追加 web_search_tools，但避免重复（_all_tools_map 中可能已包含）
            if _web_search_tools:
                existing_names = {getattr(t, "name", "") for t in tools_to_bind}
                for wst in _web_search_tools:
                    if getattr(wst, "name", "") not in existing_names:
                        tools_to_bind.append(wst)
        else:
            tools_to_bind = core_tools + _web_search_tools

        # 确保记忆工具始终在列表中（防御：某些路径可能遗漏）
        for mt in _memory_tools:
            if mt not in tools_to_bind:
                tools_to_bind.append(mt)

        # ── 最终去重（防御：不同路径可能引入重复绑定）──
        seen_names = set()
        deduped = []
        for t in tools_to_bind:
            tname = getattr(t, "name", "")
            if tname and tname not in seen_names:
                seen_names.add(tname)
                deduped.append(t)
        tools_to_bind = deduped

        # ── 知识图谱上下文（由 search_rag 节点预检索，存入 state）──
        rag_context = state.get("rag_context", "")
        if rag_context:
            # 数据已注入 prompt，不需要 LLM 再调工具搜索
            tools_to_bind = [t for t in tools_to_bind if getattr(t, "name", "") != "tool_search_knowledge"]
        logger.info(
            "RAG上下文注入: %s | 绑定工具(%d): %s",
            "有" if rag_context else "无",
            len(tools_to_bind),
            [getattr(t, "name", "") for t in tools_to_bind],
        )

        # （工具绑定延迟到调用时：运行时回退链中的每个模型都要按 tools_to_bind 单独绑定）

        if not any(isinstance(m, SystemMessage) for m in messages):
            prompt = full_prompt
            if summary:
                prompt += f"\n\n## 历史对话摘要\n{summary}"
            if specialist_suffix:
                prompt += specialist_suffix
            if rag_context:
                prompt = prompt + "\n\n" + (
                    "## 已检索到的知识库内容（请直接使用，无需再次检索）\n"
                    "以下内容已根据你的问题预先从知识库检索并注入到本条消息中。"
                    "请直接基于这些内容回答，**不要再次调用 tool_search_knowledge 或任何知识库检索工具重复查询**。\n"
                ) + rag_context

            # ── 注入被拒绝的工具调用上下文 ──
            denied = state.get("denied_tool_calls", [])
            if denied:
                denied_names = [d.get("name", "?") for d in denied]
                prompt += (
                    "\n\n## 已拒绝的操作\n"
                    f"用户已拒绝以下工具调用，**不要再次尝试**：{', '.join(denied_names)}\n"
                    "请告知用户操作已被取消，如有替代方案可提出建议。"
                )

            messages = [SystemMessage(content=prompt)] + messages

        # ── 防御：清理孤立的 tool_calls，避免 DeepSeek 400 错误 ──
        messages = _sanitize_messages_for_api(messages)

        # ── Langfuse 可观测性注入 ──
        from observability import get_callbacks
        callbacks = get_callbacks()

        # ── 语义缓存：相似问题直接返回缓存结果（跳过 LLM 调用）──
        # 图片问答跳过缓存：缓存键只有文本，跨图会串味
        _cached = None
        if user_text and not need_rpa and not _had_image:
            from cache.semantic_cache import get_cache
            _cache = get_cache()
            _cached = await _cache.get(user_text)

        if _cached and _cached.get("content"):
            logger.info("语义缓存命中，跳过 LLM 调用: %s...", user_text[:60])
            cached_msg = AIMessage(
                content=_cached["content"],
                additional_kwargs={
                    "_model_used": _cached.get("model_key", "flash"),
                    "_route_reason": route_reason,
                    "_from_cache": True,
                },
            )
            return {
                "messages": [cached_msg],
                "iteration_count": iteration,
                **extra_updates,
            }

        # ── 成本追踪：计时 + 收集 token 用量 ──
        import time as _time
        _t0 = _time.monotonic()

        # ── 运行时模型回退：主模型 API 调用失败时按序切换 ──
        # 回退链：路由选定的模型 → 另一档模型 → LLM_FALLBACK_MODEL → LLM_FALLBACK_MODEL_2
        # （创建期三级回退只覆盖"实例创建失败"，这里覆盖"运行时调用失败"）
        _fallback_chain: list[tuple[str, object]] = [
            (model_key, llm_flash if model_key == "flash" else llm_pro),
            (
                "pro" if model_key == "flash" else "flash",
                llm_pro if model_key == "flash" else llm_flash,
            ),
        ] + _runtime_fallbacks

        response = None
        used_key = model_key
        _last_error: Exception | None = None
        for _idx, (_fb_key, _fb_llm) in enumerate(_fallback_chain):
            try:
                _bound = _fb_llm.bind_tools(tools_to_bind) if tools_to_bind else _fb_llm
                response = await _bound.ainvoke(
                    messages, config={"callbacks": callbacks} if callbacks else None
                )
                used_key = _fb_key
                break
            except Exception as exc:
                _last_error = exc
                logger.warning(
                    "模型 %s 调用失败（回退链 %d/%d），尝试下一个: %s",
                    display_model_name(_fb_key), _idx + 1, len(_fallback_chain), exc,
                )
        if response is None:
            raise RuntimeError(
                f"所有模型调用均失败（起点 {display_model_name(model_key)}），最后错误: {_last_error}"
            )

        _latency = (_time.monotonic() - _t0) * 1000  # ms
        _used_model_name = display_model_name(used_key)
        if used_key != model_key:
            # 标记本次回复实际由回退模型生成，便于日志与前端区分
            route_reason = f"{route_reason} (fallback: {_used_model_name})"
            logger.info("模型回退生效: %s → %s", display_model_name(model_key), _used_model_name)

        # 提取 token 使用量
        from observability.cost import TokenUsage, CostEstimate, estimate_cost, SessionCosts
        _usage = TokenUsage.from_response(response, model=_used_model_name)
        _cost = estimate_cost(_used_model_name, _usage.prompt_tokens, _usage.completion_tokens)
        _est = CostEstimate(usage=_usage, cost_usd=_cost, latency_ms=_latency)

        # 累计 session 成本
        _prev = state.get("session_costs") or {}
        _session = SessionCosts(
            total_prompt_tokens=_prev.get("prompt_tokens", 0) + _usage.prompt_tokens,
            total_completion_tokens=_prev.get("completion_tokens", 0) + _usage.completion_tokens,
            total_cost_usd=_prev.get("cost_usd", 0.0) + _cost,
            total_llm_calls=_prev.get("llm_calls", 0) + 1,
            total_latency_ms=_prev.get("latency_ms", 0.0) + _latency,
        )

        response.additional_kwargs["_model_used"] = used_key
        response.additional_kwargs["_route_reason"] = route_reason
        response.additional_kwargs["_token_usage"] = _usage
        response.additional_kwargs["_cost_estimate"] = _cost

        # ── 安全：输出防护（PII 检测 + 脱敏）──
        if SECURITY_LLM_GUARD and response.content:
            from security.guard import get_output_guard
            try:
                out_guard = get_output_guard()
                pii_result = await out_guard.check(response.content)
                if pii_result.pii_types:
                    logger.info("OutputGuard 检测到 PII: %s", pii_result.pii_types)
                    response.content = out_guard.mask_pii(response.content)
            except Exception:
                logger.debug("OutputGuard check failed", exc_info=True)

        # ── 语义缓存：将非工具调用的最终回答写入缓存 ──
        if (
            user_text and not need_rpa and not _had_image
            and response.content and not getattr(response, "tool_calls", None)
        ):
            try:
                from cache.semantic_cache import get_cache
                _cache = get_cache()
                await _cache.put(user_text, {
                    "content": response.content,
                    "model_key": used_key,
                })
                logger.debug("语义缓存写入: %s...", user_text[:60])
            except Exception:
                logger.debug("语义缓存写入失败", exc_info=True)

        result = {
            "messages": [response],
            "session_costs": _session.to_dict(),
            "last_turn_cost": _est.to_dict(),
            "iteration_count": iteration + 1,
        }
        if extra_updates:
            result.update(extra_updates)
        return result

    async def dynamic_tool_node(state: AgentState) -> dict:
        """动态工具执行节点 — 并行执行独立的工具调用，支持 MCP 懒加载。

        LLM 返回的多个 tool_calls 之间没有数据依赖时，使用 asyncio.gather
        并发执行以降低延迟。出错时单个工具失败不影响其他工具。
        """
        from langchain_core.messages import ToolMessage
        import json as _json
        import asyncio as _asyncio

        last_msg = state["messages"][-1]
        if not hasattr(last_msg, "tool_calls") or not last_msg.tool_calls:
            return {}
        tools_by_name = {t.name: t for t in _get_all_tools()}

        # ── 安全：RBAC 工具权限检查 ──
        _user_role = state.get("user_role", "viewer")
        _blocked_names: set[str] = set()
        from auth.permissions import can_use_tool
        for _tc in last_msg.tool_calls:
            _tc_name = _tc.get("name", "") if isinstance(_tc, dict) else getattr(_tc, "name", "")
            _allowed, _reason = can_use_tool(_tc_name, _user_role)
            if not _allowed:
                logger.warning("RBAC 拒绝工具 '%s' (role=%s): %s", _tc_name, _user_role, _reason)
                _blocked_names.add(_tc_name)

        async def _invoke_one(tc: dict) -> ToolMessage:
            tool_name = tc.get("name", "")
            tool = tools_by_name.get(tool_name)
            tc_id = tc.get("id", "")
            logger.info("工具执行: %s args=%s", tool_name, _json.dumps(tc.get("args", {}), ensure_ascii=False)[:120])
            # ── 防重复检索：rag_context 已注入时，模型无需（也不应）再调 tool_search_knowledge ──
            if tool_name == "tool_search_knowledge" and state.get("rag_context"):
                logger.info("已阻止重复检索 tool_search_knowledge（rag_context 已注入，直接回退使用上下文）")
                return ToolMessage(
                    content=_json.dumps({
                        "status": "skipped",
                        "message": "知识库内容已在上下文中提供，请直接基于它回答，无需再次检索。",
                    }),
                    tool_call_id=tc_id, name=tool_name,
                )
            if tool_name in _blocked_names:
                return ToolMessage(
                    content=_json.dumps({"status": "error", "message": f"权限不足: {tool_name} 需更高权限"}),
                    tool_call_id=tc_id, name=tool_name,
                )
            if tool is None:
                return ToolMessage(
                    content=_json.dumps({"status": "error", "message": f"Unknown tool: {tool_name}"}),
                    tool_call_id=tc_id, name=tool_name,
                )
            try:
                result = await tool.ainvoke(tc.get("args", {}))
            except Exception as exc:
                result = {"status": "error", "message": str(exc)}

            if not isinstance(result, ToolMessage):
                content = result if isinstance(result, str) else _json.dumps(result, ensure_ascii=False)
                result = ToolMessage(content=content, tool_call_id=tc_id, name=tool_name)
            return result

        # 并行执行所有独立的工具调用
        coroutines = [_invoke_one(tc) for tc in last_msg.tool_calls]
        results = await _asyncio.gather(*coroutines)
        return {"messages": list(results)}

    async def sub_agent_node(state: AgentState) -> dict:
        """子代理 LLM 节点（specialist 子图内复用 call_model 的思考-行动循环）。

        职责：
        - 置位 _subgraph_active，让 call_model 跳过 RPA 按需注入（子代理不绑 RPA 工具）。
        - 子代理独立迭代上限保护（SUBAGENT_MAX_ITERATIONS），超限强制终止并返回封顶提示，
          避免单次委派长时间占用主线程。
        """
        iteration = state.get("iteration_count", 0)
        if iteration >= SUBAGENT_MAX_ITERATIONS:
            logger.warning("子代理达到最大迭代次数 %d，强制终止", SUBAGENT_MAX_ITERATIONS)
            return {
                "messages": [AIMessage(content=(
                    f"已达到子代理处理上限（{SUBAGENT_MAX_ITERATIONS}轮思考-行动）。"
                    "以下为阶段性结果；如需继续，可由主代理接手处理。"
                ))],
            }
        _subgraph_active["on"] = True
        try:
            return await call_model(state)
        finally:
            _subgraph_active["on"] = False

    tool_catalog_text = _build_tool_catalog()

    # ── 意图分类 + 工具评分节点（Flash）──

    async def classify_intent(state: AgentState) -> dict:
        """用 Flash 做意图分类 + 工具评分。

        关键：使用原生 openai SDK 调 LLM，而非 LangChain ChatOpenAI。
        这样 LangGraph 的 messages 流模式不会捕获此调用，避免分类器 JSON
        泄漏到用户输出中（此前用 ChatOpenAI.ainvoke 时，LangGraph 会将其
        当作 AIMessageChunk 流式发送给前端）。
        """
        user_text = extract_user_text(state.get("messages", []))
        if not user_text:
            return {"intent": "trivial", "selected_tools": [], "needs_rag": False}

        prompt = CLASSIFIER_SYSTEM + "\n\n" + CLASSIFIER_USER.format(
            tool_catalog=tool_catalog_text,
            user_query=user_text,
        )

        import json as _json
        from config import LLM_FLASH_MODEL

        intent = "complex"
        tools: list[str] = []
        needs_rag = True

        # 用原生 openai SDK 直调，避免 LangChain 回调系统将分类器输出注入消息流
        from .client_factory import get_async_openai_client
        client = get_async_openai_client()

        try:
            resp = await client.chat.completions.create(
                model=LLM_FLASH_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
            result = _json.loads(text)
            intent = result.get("intent", "complex")
            tools = result.get("tools", [])
            needs_rag = result.get("needs_rag", False)
        except Exception:
            logger.debug("Intent classification failed, defaulting to complex", exc_info=True)

        # 按意图分组补充默认工具
        if intent != "trivial":
            group_tools = INTENT_TOOL_GROUPS.get(intent, [])
            for tn in group_tools:
                if tn not in tools:
                    tools.append(tn)
            for tn in _MEMORY_TOOLS:
                if tn not in tools:
                    tools.append(tn)

        # 验证工具名是否存在
        tb_name = _get_tool_by_name()
        valid_tools = [tn for tn in tools if tn in tb_name]
        invalid = set(tools) - set(valid_tools)
        if invalid:
            logger.debug("Classifier suggested unknown tools: %s", invalid)

        # 按意图懒加载 MCP 服务
        from agent.mcp_setup import ensure_mcp_for_intent
        await ensure_mcp_for_intent(intent)

        logger.info("Intent: %s, Tools: %s, RAG: %s", intent, valid_tools, needs_rag)
        return {"intent": intent, "selected_tools": valid_tools, "needs_rag": needs_rag}

    # ── Supervisor 节点（多 Agent 协作）──
    # 路由函数 _route_after_supervisor 已在模块级定义（真 specialist → run_specialist 子图）

    async def supervisor_node(state: AgentState) -> dict:
        """Supervisor 节点：分析复杂任务并委派给专业子 Agent。

        使用 Flash LLM 分析用户请求，决定由哪个 specialist 处理，
        并将结果写入 state.specialist / specialist_task / specialist_history。
        """
        from datetime import datetime, timezone
        import json as _json
        from config import LLM_FLASH_MODEL

        user_text = extract_user_text(state.get("messages", []))
        if not user_text:
            return {"specialist": "general", "specialist_task": user_text or ""}

        specialist = "general"
        reason = ""
        sub_tasks: list[str] = []

        try:
            from .client_factory import get_async_openai_client
            client = get_async_openai_client()
            resp = await client.chat.completions.create(
                model=LLM_FLASH_MODEL,
                messages=[
                    {"role": "system", "content": SUPERVISOR_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=0,
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            result_text = resp.choices[0].message.content or "{}"
            result = extract_first_json(result_text)
            if isinstance(result, dict):
                specialist = result.get("specialist", "general")
                reason = result.get("reason", "")
                sub_tasks = result.get("sub_tasks", [])

            # 验证 specialist 名称
            if specialist not in SPECIALISTS:
                specialist = "general"
                reason = f"Unknown specialist, fallback to general"
        except Exception:
            logger.debug("Supervisor classification failed, defaulting to general", exc_info=True)
            specialist = "general"
            reason = "Supervisor unavailable, fallback to general"

        # 构建子任务文本（供 planner 使用）
        task_text = user_text
        if sub_tasks:
            task_text = f"主任务: {user_text}\n子任务:\n" + "\n".join(
                f"{i+1}. {st}" for i, st in enumerate(sub_tasks)
            )

        # 记录委派历史
        history = list(state.get("specialist_history", []))
        history.append({
            "specialist": specialist,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        sp = get_specialist(specialist)
        display = sp.display_name if sp else "通用助手"
        logger.info(
            "Supervisor: delegated to '%s' (%s) — reason: %s — %d sub-tasks",
            specialist, display, reason, len(sub_tasks),
        )

        result = {
            "specialist": specialist,
            "specialist_task": task_text,
            "specialist_history": history,
        }
        # 真子代理（走子图）→ 发射"子代理开始"瞬态标记，前端显示运行中 chip
        if specialist in SUBAGENT_NAMES:
            result["specialist_started"] = {
                "specialist": specialist,
                "name": display,
                "icon": sp.icon if sp else "",
            }
        return result

    # ── RAG 检索节点（独立节点，可被 checkpoint 追踪和独立重试）──

    # ── 查询改写节点 ──

    _QUERY_REWRITE_SYSTEM = """你是一个查询改写助手。判断用户输入是否为上下文相关的追问。

如果是追问（代词指代、省略主语），将追问改写为完整、独立的查询。
如果不是追问（独立的新问题），原样返回用户的输入。

## 规则
- 只有明确是追问时才改写：代词指代（"它"、"这个"、"那个"）、省略、追问（"再详细说说"、"然后呢"）
- 改写时保留用户的原始意图，不要添加额外信息
- 不是追问就直接返回原输入，不要修改
- 返回格式: {"is_followup": true|false, "rewritten": "改写后的查询"}
"""

    async def query_rewrite_node(state: AgentState) -> dict:
        """检测多轮追问并改写为完整查询，在 search_rag 之前运行。

        使用 Flash LLM 做轻量判断：如果是上下文相关的追问则改写，
        否则原样返回。改写后的查询存入 state.rewritten_query，
        search_rag_node 使用改写后的查询做检索。
        """
        user_text = extract_user_text(state.get("messages", []))
        if not user_text or len(user_text) < 3:
            return {"rewritten_query": user_text or ""}

        # 只有多轮对话才需要改写检测
        messages = state.get("messages", [])
        user_msgs = [m for m in messages if isinstance(m, HumanMessage)]
        if len(user_msgs) <= 1:
            return {"rewritten_query": user_text}

        try:
            from .client_factory import get_async_openai_client
            from config import LLM_FLASH_MODEL

            client = get_async_openai_client()

            # 获取最近 3 轮对话上下文
            recent_context = []
            for m in messages[-8:]:
                role = "user" if isinstance(m, HumanMessage) else "assistant"
                content = content_to_text(m.content)
                if content.strip():
                    recent_context.append(f"[{role}]: {content[:300]}")

            context_str = "\n".join(recent_context) if recent_context else "无上下文"

            resp = await client.chat.completions.create(
                model=LLM_FLASH_MODEL,
                messages=[
                    {"role": "system", "content": _QUERY_REWRITE_SYSTEM},
                    {"role": "user", "content": f"对话上下文:\n{context_str}\n\n当前用户输入: {user_text}"},
                ],
                temperature=0,
                max_tokens=200,
                response_format={"type": "json_object"},
            )

            result_text = resp.choices[0].message.content or "{}"
            from .utils import extract_first_json
            parsed = extract_first_json(result_text)
            if isinstance(parsed, dict) and parsed.get("is_followup"):
                rewritten = parsed.get("rewritten", user_text)
                if rewritten and rewritten != user_text:
                    logger.info("查询改写: '%s' → '%s'", user_text[:60], rewritten[:60])
                    return {"rewritten_query": rewritten}

        except Exception:
            logger.debug("查询改写失败，使用原始查询", exc_info=True)

        return {"rewritten_query": user_text}

    # ── RAG 检索节点 ──

    async def search_rag_node(state: AgentState) -> dict:
        """从知识图谱中检索相关内容，结果存入 rag_context 供后续节点使用。

        独立于 agent 节点运行，失败不阻断后续流程。
        每个检索结果附带唯一引用 ID（[1], [2], ...），便于 LLM 在回答中引用来源。
        """
        user_text = state.get("rewritten_query", "") or extract_user_text(state.get("messages", []))
        if not user_text:
            return {"rag_context": "", "rag_citations": []}

        try:
            rag_result = await async_search_knowledge(user_text, top_k=RAG_TOP_K, mode=RAG_MODE, use_rerank=False)
            if rag_result.get("status") == "success":
                results = rag_result.get("results", [])
                if results:
                    # 构建带引用 ID 的上下文和引用元数据列表
                    context_parts: list[str] = []
                    citations: list[dict] = []
                    context_parts.append("## 知识图谱参考内容")
                    context_parts.append(
                        "以下是从知识图谱中查询到的数据，"
                        "请参考这些信息回答用户问题。"
                        "在每个引用处使用 [N] 标记来源。\n"
                    )

                    for i, item in enumerate(results):
                        ref_id = i + 1
                        content = item.get("content", "")
                        source = item.get("source", "知识库")
                        score = item.get("score", 0.0)

                        if content:
                            context_parts.append(
                                f"[{ref_id}] {content}"
                                f"（来源: {source}, 相关度: {score:.2f}）"
                            )
                            citations.append({
                                "index": ref_id,
                                "source": source,
                                "content_snippet": content[:200],
                                "relevance_score": score if isinstance(score, (int, float)) else 0.0,
                            })

                    rag_context = "\n\n".join(context_parts)
                    logger.info("RAG search: %d results, %d chars, %d citations",
                                len(results), len(rag_context), len(citations))
                    return {"rag_context": rag_context, "rag_citations": citations}
        except Exception:
            logger.warning("RAG 检索失败", exc_info=True)

        return {"rag_context": "", "rag_citations": []}

    # ── Planner 节点 ──

    PLANNER_SYSTEM = """你是一个任务规划器。分析用户请求，生成结构化的执行计划。

## 输出格式（严格 JSON）
{
  "goal": "一句话描述目标",
  "steps": [
    {
      "step": 1,
      "description": "步骤描述",
      "tool_hint": "可能用到的工具名（如不确定用 ?）",
      "expected_output": "预期产出描述"
    }
  ],
  "estimated_rounds": 3
}

## 规则
- 步骤应具体、可执行、可验证
- 每步应明确需要什么工具
- 复杂任务拆分为 2-6 步
- 简单任务给 1 步即可
- 只输出 JSON，不要其他内容"""

    async def planner_node(state: AgentState) -> dict:
        user_text = extract_user_text(state.get("messages", []))
        if not user_text:
            return {"plan": "", "plan_steps": [], "plan_index": 0, "replan_count": 0}

        import json as _json

        try:
            response = await llm_flash_zero.ainvoke([
                SystemMessage(content=PLANNER_SYSTEM),
                HumanMessage(content=f"用户请求: {user_text}"),
            ])
            plan_data = extract_first_json(str(response.content))
            if plan_data:
                steps = plan_data.get("steps", [])
                # 初始化所有步骤状态
                for s in steps:
                    s["status"] = "pending"

                # 生成人类可读的计划文本（保持向后兼容）
                plan_text = f"## 目标\n{plan_data.get('goal', '')}\n\n## 步骤\n"
                for s in steps:
                    plan_text += f"{s['step']}. {s['description']} `[{s.get('tool_hint', '?')}]`\n"
                plan_text += f"\n> 预估 {plan_data.get('estimated_rounds', len(steps))} 轮完成"

                logger.info(
                    "Planning: %d steps generated — %s",
                    len(steps),
                    [s.get("description", "")[:40] for s in steps],
                )
                return {
                    "plan": plan_text,
                    "plan_steps": steps,
                    "plan_index": 0,
                    "replan_count": 0,
                }
        except Exception as exc:
            logger.warning("Structured planning failed: %s", exc)

        # 降级：返回纯文本计划
        plan_prompt = (
            "你是一个任务规划器。分析以下用户请求，制定一个清晰的执行计划。\n\n"
            "## 目标\n(一句话描述要完成的任务)\n\n"
            "## 执行步骤\n1. (第一步)\n2. (第二步)\n\n"
            "## 预估工具\n(列出可能需要用到的工具)\n\n"
            f"用户请求: {user_text}"
        )
        try:
            response = await llm_flash_zero.ainvoke([SystemMessage(content=plan_prompt)])
            plan_text = str(response.content)
            logger.info("Planning fallback: text plan (%d chars)", len(plan_text))
            return {"plan": plan_text, "plan_steps": [], "plan_index": 0, "replan_count": 0}
        except Exception as exc2:
            logger.warning("Planning failed entirely: %s", exc2)
            return {"plan": "", "plan_steps": [], "plan_index": 0, "replan_count": 0}

    # ── Graceful Degradation：反思节点 ──

    async def reflect_node(state: AgentState) -> dict:
        messages = list(state["messages"])
        last_error = ""
        for m in reversed(messages):
            if m.type == "tool":
                content = m.content
                if isinstance(content, dict):
                    last_error = content.get("message", str(content)[:300])
                elif isinstance(content, str):
                    import json as _json
                    try:
                        parsed = _json.loads(content)
                        last_error = parsed.get("message", content[:300])
                    except Exception:
                        last_error = content[:300]
                break

        reflection_text = (
            "The last tool call returned an error. Analyze the error and "
            "call the tool again with corrected arguments.\n\n"
            f"Tool error: {last_error}\n\n"
            "Check: parameter types, required fields, prerequisites.\n"
            "If you have already retried once, explain the error to the user."
        )
        msgs = messages + [SystemMessage(content=reflection_text)]
        # 防御：清理可能存在的孤立的 tool_calls
        msgs = _sanitize_messages_for_api(msgs)
        response = await llm_flash_zero.ainvoke(msgs)
        response.additional_kwargs["_node"] = "reflect"
        return {
            "messages": [response],
            "tool_retries": state.get("tool_retries", 0) + 1,
        }

    # ── Human-in-the-Loop：审批节点 ──

    async def check_approval_node(state: AgentState) -> dict:
        """高风险工具调用的审批门控节点。

        在工具执行前检查是否需要人工审批：
        - 低风险工具 → 直接放行到 tools 节点
        - 高风险 + 未审批 → interrupt() 暂停执行，等待人工决策
        - 已批准 → 放行
        - 已拒绝 → 生成 synthetic ToolMessage（status: denied），跳回 agent

        审批模式由 APPROVAL_MODE 环境变量控制：
        - "off": 全部自动执行
        - "standard": 仅高风险工具审批（默认）
        - "strict": 除显式标记为 low 的工具外都需要审批
        """
        import json as _json
        from langchain_core.messages import ToolMessage

        approval_mode = get_approval_mode()
        if approval_mode == "off":
            return {"approval_decision": ""}

        decision = state.get("approval_decision", "")

        # 已处理的审批决定 → 清理状态
        if decision == "approved":
            return {"approval_decision": "", "pending_approval": None}

        if decision == "denied":
            # 为所有被拒绝的工具调用生成 ToolMessage
            pending = state.get("pending_approval") or {}
            denied_calls = pending.get("calls", [])
            denied_messages = []
            for dc in denied_calls:
                denied_messages.append(ToolMessage(
                    content=_json.dumps({
                        "status": "denied",
                        "message": "用户拒绝了此操作",
                    }, ensure_ascii=False),
                    tool_call_id=dc.get("id", ""),
                    name=dc.get("name", "unknown"),
                ))
            # 记录被拒绝的调用，供 agent 节点的 LLM 上下文注入使用
            prev_denied = list(state.get("denied_tool_calls", []))
            prev_denied.extend(denied_calls)
            return {
                "approval_decision": "",
                "pending_approval": None,
                "messages": denied_messages,
                "denied_tool_calls": prev_denied,
            }

        # 检查最后一条消息是否有需要审批的工具调用
        last_msg = state["messages"][-1]
        if not (hasattr(last_msg, "tool_calls") and last_msg.tool_calls):
            return {"approval_decision": "", "pending_approval": None}

        # 收集高风险工具调用
        risky_calls = []
        for tc in last_msg.tool_calls:
            tool_name = tc.get("name", "")
            needs, level, reason = classify_tool_risk(tool_name, mode=approval_mode)
            if needs:
                # 命令级策略：命中 forbidden 命令时单点升级原因（prefix_rule 引擎）
                forbidden, forbid_reason = check_command_policy(tc.get("args", {}))
                if forbidden:
                    reason = forbid_reason
                risky_calls.append({
                    "id": tc.get("id", ""),
                    "name": tool_name,
                    "args": tc.get("args", {}),
                    "risk_level": level,
                    "reason": reason,
                })

        if not risky_calls:
            # 无高风险工具，直接放行
            return {"approval_decision": "", "pending_approval": None}

        # 构建审批 payload 并中断执行
        payload = build_approval_payload(risky_calls)
        logger.info(
            "Approval required: %d risky tool(s) — %s",
            len(risky_calls),
            [c["name"] for c in risky_calls],
        )

        # interrupt() 暂停图执行，等待外部 Command(resume=...) 恢复
        user_decision = interrupt(payload)

        # 接受 "approve"/"approved" 和 "deny"/"denied"（兼容 REST API 和 WebSocket 两种调用约定）
        if user_decision in ("approve", "approved"):
            logger.info("Approval granted by user")
            return {"approval_decision": "approved", "pending_approval": payload}
        else:
            logger.info("Approval denied by user (decision=%s)", user_decision)
            # 立即生成 denied ToolMessages，避免多次循环
            denied_messages = []
            denied_records = []
            for dc in risky_calls:
                denied_messages.append(ToolMessage(
                    content=_json.dumps({
                        "status": "denied",
                        "message": "用户拒绝了此操作",
                    }, ensure_ascii=False),
                    tool_call_id=dc.get("id", ""),
                    name=dc.get("name", "unknown"),
                ))
                denied_records.append({
                    "id": dc.get("id", ""),
                    "name": dc.get("name", "unknown"),
                    "args": dc.get("args", {}),
                    "risk_level": dc.get("risk_level", "high"),
                    "reason": dc.get("reason", ""),
                })
            # 合并历史被拒绝的调用
            prev_denied = list(state.get("denied_tool_calls", []))
            prev_denied.extend(denied_records)
            return {
                "approval_decision": "",
                "pending_approval": None,
                "messages": denied_messages,
                "denied_tool_calls": prev_denied,
            }

    # ── Plan-Execute-Replan：计划进度追踪 ──

    async def plan_check_node(state: AgentState) -> dict:
        """评估当前计划步骤的执行进度，标记完成/失败，检测是否需要重规划。

        在 tool → track_failures 之后运行，根据最近的工具结果更新步骤状态。
        没有计划步骤时直接透传。
        """
        steps = list(state.get("plan_steps", []))
        if not steps:
            return {}

        plan_idx = state.get("plan_index", 0)
        failures = state.get("tool_failures", 0)

        # 扫描最近的消息判断当前步骤是否成功
        messages = state.get("messages", [])
        last_tool_success = False
        last_tool_found = False
        for m in reversed(messages):
            if hasattr(m, "type") and m.type == "tool":
                last_tool_found = True
                last_tool_success = not is_tool_error(m)
                break
            elif hasattr(m, "type") and m.type == "ai" and getattr(m, "tool_calls", None):
                # LLM 发出了工具调用但还没有结果 → 步骤进行中
                last_tool_found = True
                last_tool_success = True  # 标记为成功，不阻断
                break

        # 更新第一个 pending 步骤的状态
        for s in steps:
            if s.get("status") == "pending":
                if last_tool_found and last_tool_success:
                    s["status"] = "done"
                elif failures >= 2:
                    s["status"] = "failed"
                else:
                    s["status"] = "in_progress"
                break

        # 计算整体状态
        all_done = all(s.get("status") in ("done", "failed") for s in steps)
        any_failed = any(s.get("status") == "failed" for s in steps)
        done_count = sum(1 for s in steps if s.get("status") == "done")
        failed_count = sum(1 for s in steps if s.get("status") == "failed")

        if done_count + failed_count > 0:
            logger.info(
                "Plan progress: %d/%d steps (done=%d failed=%d) all_done=%s",
                done_count + failed_count, len(steps), done_count, failed_count, all_done,
            )

        return {
            "plan_steps": steps,
            "plan_index": plan_idx + 1,
        }

    async def replan_node(state: AgentState) -> dict:
        """基于已执行步骤和失败情况，用 LLM 重新生成剩余计划。

        保留已完成的步骤，仅替换 pending/failed 步骤为新计划。
        超过最大重规划次数后放弃剩余步骤。
        """
        steps = list(state.get("plan_steps", []))
        replan_count = state.get("replan_count", 0)

        if replan_count >= _MAX_REPLAN:
            logger.warning("Max replan limit (%d) reached, abandoning remaining steps", _MAX_REPLAN)
            # 标记所有 pending/failed 为 skipped
            for s in steps:
                if s.get("status") in ("pending", "failed", "in_progress"):
                    s["status"] = "skipped"
            return {"plan_steps": steps, "replan_count": replan_count}

        done_steps = [s for s in steps if s.get("status") == "done"]
        failed_steps = [s for s in steps if s.get("status") == "failed"]
        pending_steps = [s for s in steps if s.get("status") == "pending"]

        user_text = extract_user_text(state.get("messages", []))

        import json as _json
        replan_prompt = f"""原计划执行情况：
- 已完成 {len(done_steps)} 步: {_json.dumps([s['description'] for s in done_steps], ensure_ascii=False)}
- 失败 {len(failed_steps)} 步: {_json.dumps([s['description'] for s in failed_steps], ensure_ascii=False)}
- 待执行 {len(pending_steps)} 步

原始请求: {user_text}

请根据执行情况调整剩余计划。输出 JSON:
{{"goal": "调整后的目标", "steps": [{{"step": 1, "description": "...", "tool_hint": "?", "expected_output": "..."}}], "estimated_rounds": 2}}
只输出 JSON，不要其他内容。"""

        try:
            response = await llm_flash_zero.ainvoke([SystemMessage(content=replan_prompt)])
            new_plan = extract_first_json(str(response.content))
            if new_plan:
                new_steps = new_plan.get("steps", [])
                # 重新编号
                for i, s in enumerate(new_steps):
                    s["step"] = len(done_steps) + i + 1
                    s["status"] = "pending"
                merged = list(done_steps) + new_steps
                logger.info(
                    "Replan #%d: %d done + %d new steps",
                    replan_count + 1, len(done_steps), len(new_steps),
                )
                return {
                    "plan_steps": merged,
                    "replan_count": replan_count + 1,
                    "tool_retries": 0,
                    "plan_index": 0,
                }
        except Exception as exc:
            logger.warning("Replan failed: %s", exc)

        # 降级：放弃剩余步骤
        for s in steps:
            if s.get("status") in ("pending", "failed", "in_progress"):
                s["status"] = "skipped"
        return {"plan_steps": steps, "replan_count": replan_count + 1}

    # ── 结构化输出：finalize 节点 ──

    async def finalize_node(state: AgentState) -> dict:
        """将 Agent 的自由文本响应转换为结构化的类型安全输出。

        仅当 state.response_schema 被设置时才执行结构化转换。
        否则透传（保留原始文本响应）。
        """
        schema = state.get("response_schema")
        if not schema:
            return {}

        schema_type = schema.get("type", "text")
        from .response_schemas import RESPONSE_SCHEMAS
        response_model = RESPONSE_SCHEMAS.get(schema_type)
        if response_model is None:
            logger.warning("Unknown response schema type: %s", schema_type)
            return {}

        # 从消息历史中提取 Agent 最终文本响应
        final_text = ""
        for m in reversed(state.get("messages", [])):
            if isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                final_text = str(m.content)
                break

        if not final_text:
            return {}

        try:
            structured_llm = llm_flash.with_structured_output(response_model)
            result = await structured_llm.ainvoke([
                SystemMessage(
                    content=f"将以下回复转换为 {schema_type} 格式的结构化数据。"
                ),
                HumanMessage(content=final_text),
            ])
            if hasattr(result, "model_dump"):
                structured = result.model_dump()
            elif isinstance(result, dict):
                structured = result
            else:
                structured = {"response_type": schema_type, "content": str(result)}

            logger.info(
                "Structured output generated: type=%s keys=%s",
                schema_type, list(structured.keys()),
            )
            return {"structured_response": structured}
        except Exception as exc:
            logger.warning("Structured output conversion failed: %s", exc)
            return {"structured_response": {"response_type": "text", "content": final_text}}

    # ── 子代理子图（真 Sub-Agent）+ run_specialist 父节点 ──
    # 复用同一批节点闭包（call_model/dynamic_tool_node/reflect_node/_track_failures），
    # 独立编译无 checkpointer 的子图，由 run_specialist 命令式 ainvoke 执行。

    _subgraph = build_specialist_subgraph(
        agent_node=sub_agent_node,
        tool_node=dynamic_tool_node,
        reflect_node=reflect_node,
        track_failures=_track_failures,
        check_approval_node=sub_check_approval,
    )

    async def run_specialist(state: AgentState) -> dict:
        """run_specialist 父节点：命令式执行 specialist 子图，合并结果回父 state。

        - 子图无 checkpointer、串行执行；报告以预构造 AIMessage 追加进父 messages，
          经 stream_mode=["messages"] 自动流式发射到前端。
        - 子代理 session_costs 独立累计，完成后 _merge_session_costs 合并进父 state。
        - 异常不崩溃：捕获后返回失败报告 + status="failed"，主代理以 general 收尾。
        """
        specialist = state.get("specialist", "")
        task = state.get("specialist_task", "")
        sp = get_specialist(specialist)
        display = sp.display_name if sp else specialist
        icon = sp.icon if sp else ""

        sub_input = {
            "messages": [HumanMessage(content=task or "请完成委派的任务")],
            "specialist": specialist,
            "user_role": state.get("user_role", "viewer"),
            "intent": "complex",
            "selected_tools": [],
            "needs_rag": False,
            "session_costs": {},
            "iteration_count": 0,
            "tool_failures": 0,
            "tool_retries": 0,
            "conversation_summary": "",
            "denied_tool_calls": [],
        }

        sub_cost: dict = {}
        status = "failed"
        try:
            result = await _subgraph.ainvoke(sub_input, config={"recursion_limit": 60})
            sub_cost = result.get("session_costs") or {}
            report = _extract_subagent_report(result.get("messages", []))
            status = "done" if report else "failed"
            report = report or "子代理未产出有效报告，请向用户说明并询问是否需要重试。"
        except Exception as exc:
            logger.exception("子代理 '%s' 执行失败: %s", specialist, exc)
            report = (
                f"子代理（{display}）执行时出现异常，未能完成任务。\n"
                f"错误信息：{str(exc)[:500]}\n"
                "请告知用户并询问是否需要重试。"
            )

        # 合并子代理成本到父 session_costs
        _merged = _merge_session_costs(state.get("session_costs") or {}, sub_cost)

        return {
            "messages": [AIMessage(
                content=report,
                additional_kwargs={"_node": "run_specialist", "_specialist": specialist},
            )],
            "specialist_results": [{
                "specialist": specialist,
                "task": task,
                "status": status,
                "report": report,
                "cost": sub_cost,
            }],
            "specialist_report": {
                "specialist": specialist,
                "name": display,
                "icon": icon,
                "report": report,
            },
            "specialist": "general",     # 清空委派，主代理以 general 收尾（可再走审批执行高风险操作）
            "specialist_task": "",
            "session_costs": _merged,
            "last_turn_cost": sub_cost,
        }

    # ── 图拓扑 ──

    graph = StateGraph(AgentState)
    graph.add_node("planner", planner_node)
    graph.add_node("agent", call_model)
    graph.add_node("check_approval", check_approval_node)
    graph.add_node("tools", dynamic_tool_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("track_failures", _track_failures)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("query_rewrite", query_rewrite_node)
    graph.add_node("search_rag", search_rag_node)
    graph.add_node("plan_check", plan_check_node)
    graph.add_node("replan", replan_node)
    graph.add_node("run_specialist", run_specialist)
    graph.add_node("finalize", finalize_node)

    # 入口 → 意图分类
    graph.set_entry_point("classify_intent")
    # 分类后路由：complex → supervisor（多 Agent），RAG → query_rewrite，其余 → agent
    graph.add_conditional_edges(
        "classify_intent", _route_after_classify,
        {"supervisor": "supervisor", "query_rewrite": "query_rewrite", "agent": "agent"},
    )
    graph.add_edge("query_rewrite", "search_rag")
    # RAG 检索完成后：complex → supervisor → planner → agent
    graph.add_conditional_edges(
        "search_rag", _route_after_rag,
        {"supervisor": "supervisor", "agent": "agent"},
    )
    # supervisor → run_specialist（真子代理子图）| planner（规划后再执行）| agent
    graph.add_conditional_edges(
        "supervisor", _route_after_supervisor,
        {"run_specialist": "run_specialist", "planner": "planner", "agent": "agent"},
    )
    graph.add_edge("planner", "agent")
    # 子图执行完成后，主代理以 general 收尾（可基于报告继续对话 / 触发审批执行高风险操作）
    graph.add_edge("run_specialist", "agent")
    # agent → 审批门控 / 反思 / 结构化输出
    graph.add_conditional_edges(
        "agent", _should_continue,
        {"tools": "check_approval", "reflect": "reflect", "__end__": "finalize"},
    )
    # 审批后路由：批准 → 执行工具，拒绝 → 回到 agent
    graph.add_conditional_edges(
        "check_approval", _route_after_approval,
        {"tools": "tools", "agent": "agent"},
    )
    graph.add_edge("tools", "track_failures")
    # Plan-Execute-Replan 循环
    graph.add_edge("track_failures", "plan_check")
    graph.add_conditional_edges(
        "plan_check", _route_after_plan_check,
        {"replan": "replan", "agent": "agent", "__end__": "finalize"},
    )
    graph.add_conditional_edges(
        "replan", _route_after_replan,
        {"agent": "agent", "__end__": "finalize"},
    )
    graph.add_edge("reflect", "agent")
    graph.add_edge("finalize", END)

    checkpointer = await _create_checkpointer()
    return graph.compile(checkpointer=checkpointer)


async def _close_checkpointer(agent) -> None:
    """安全关闭 Agent 的 checkpointer 连接。"""
    try:
        checkpointer = getattr(agent, "checkpointer", None)
        if checkpointer is None:
            return
        # SQLite saver (close conn)
        if hasattr(checkpointer, "_conn") and checkpointer._conn is not None:
            await checkpointer._conn.close()
            logger.info("Closed previous SQLite checkpointer")
        # PostgreSQL saver (close pool)
        elif hasattr(checkpointer, "_pool") and checkpointer._pool is not None:
            await checkpointer._pool.close()
            logger.info("Closed previous checkpointer pool")
    except Exception:
        pass


# ── Agent 实例池：按 context_summary 隔离，避免并发连接互相影响 ──
_MAX_CACHED_AGENTS = 20
_agent_cache: dict[str, object] = {}
# B6：并发保护 — 无锁时多个连接同时 cache miss 会重复 build agent（checkpointer 冲突）
_agent_cache_lock = asyncio.Lock()


async def get_agent(context_summary: str = ""):
    """获取编译好的 Agent 实例，按 context 缓存。

    不同 context 的 Agent 独立持有自己的 checkpointer，避免一个连接的
    context 切换导致另一个正在运行的连接 checkpointer 被关闭。
    """
    key = context_summary or "__default__"

    async with _agent_cache_lock:
        if key in _agent_cache:
            return _agent_cache[key]

        # 限制缓存大小：超过上限时驱逐最早未使用的
        while len(_agent_cache) >= _MAX_CACHED_AGENTS:
            oldest_key = next(iter(_agent_cache))
            old_agent = _agent_cache.pop(oldest_key)
            await _close_checkpointer(old_agent)
            logger.info("Agent 缓存满，已驱逐: %s", oldest_key[:30])

        agent = await build_agent(context_summary)
        _agent_cache[key] = agent
        _sync_app_context(agent, context_summary)
        return agent


async def rebuild_agent(context_summary: str = ""):
    """强制重建 Agent 实例（用于切换用户、配置变更等场景）。"""
    key = context_summary or "__default__"

    async with _agent_cache_lock:
        old_agent = _agent_cache.pop(key, None)
        if old_agent is not None:
            await _close_checkpointer(old_agent)

        agent = await build_agent(context_summary)
        _agent_cache[key] = agent
        _sync_app_context(agent, context_summary)
        return agent


def _sync_app_context(agent, context_summary: str) -> None:
    """同步 AppContext._agent（B10 修复）。

    历史原因：threads/approval 路由从 AppContext 读取 agent，而 AppContext.set_agent
    从未被调用 → 永远是 None → 历史消息永远返回空数组。此处每次构建后同步，
    使两条全局状态路径指向同一实例（所有实例共享同一 SQLite checkpoint 文件）。
    """
    try:
        from app_context import get_app_context
        get_app_context().set_agent(agent, context_summary)
    except Exception:
        logger.debug("Failed to sync AppContext agent", exc_info=True)


async def get_checkpoint_agent():
    """获取可读取 checkpoint 的 agent 实例。

    优先复用 _agent_cache 中任意实例（所有实例共享同一 SQLite checkpoint 文件，
    可跨实例按 thread_id 读取状态）；冷启动缓存为空时构建默认实例 ——
    重启后旧对话历史仍可从磁盘 checkpoint 恢复。
    """
    if _agent_cache:
        return next(iter(_agent_cache.values()))
    return await get_agent()
