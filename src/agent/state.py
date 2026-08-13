"""LangGraph Agent 状态定义。

所有新增字段均为可选（运行时由 graph 节点填充默认值），
与现有 checkpoint 和 input_state 向后兼容。
"""

from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    # ── 消息历史（LangGraph 自动合并，实际必需但 TypedDict 层面标记为可选）──
    messages: Annotated[list, add_messages]

    # ── 规划 ──
    plan: str                           # 计划文本（Markdown，保持向后兼容）
    plan_steps: list[dict]              # 结构化计划步骤 [{step, description, tool_hint, status}]
    plan_index: int                     # 当前执行步索引（0-based）
    replan_count: int                   # 已重规划次数

    # ── 工具执行追踪 ──
    tool_failures: int                  # 连续失败计数
    tool_retries: int                   # 重试次数（reflect 触发后 +1）

    # ── 路由字段 ──
    intent: str                         # trivial | knowledge | web | file | code | rpa | time | complex
    selected_tools: list[str]           # Flash 分类器选中的工具名列表
    needs_rag: bool                     # 是否需要检索知识库
    rag_context: str                    # RAG 检索结果上下文
    rag_citations: list[dict]           # RAG 检索来源引用 [{index, source, content_snippet, relevance_score}]

    # ── Human-in-the-Loop 审批 ──
    pending_approval: dict | None       # 当前待审批的工具调用信息
    approval_decision: str              # "" | "approved" | "denied"
    denied_tool_calls: list[dict]       # 本次对话中被拒绝的工具调用记录

    # ── 结构化输出 ──
    response_schema: dict | None        # 请求的结构化输出格式 {"type": "table"|"text"|...}
    structured_response: dict | None    # 最终结构化响应（由 finalize 节点填充）

    # ── 成本追踪 ──
    session_costs: dict | None          # 累计成本 {prompt_tokens, completion_tokens, cost_usd, llm_calls, latency_ms}
    last_turn_cost: dict | None         # 最近一轮调用的成本详情
    iteration_count: int                # 当前 Agent 迭代计数（防止无限循环）

    # ── 查询改写 ──
    rewritten_query: str                # 多轮追问改写后的完整查询

    # ── 对话摘要 ──
    conversation_summary: str           # 长对话压缩后的结构化摘要

    # ── 多 Agent 协作（Supervisor 模式）──
    specialist: str                     # 当前活跃的 specialist: researcher|coder|analyst|general
    specialist_history: list[dict]      # 已委派的 specialist 记录 [{specialist, reason, timestamp}]
    specialist_task: str                # supervisor 分解的子任务描述

    # ── 安全 / RBAC ──
    user_role: str                      # 当前用户角色: admin|editor|viewer（默认 viewer）
