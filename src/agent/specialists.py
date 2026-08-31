"""多 Agent 协作 — 专业子 Agent 定义 + Supervisor 路由。

Supervisor 分析复杂任务 → 委派给 specialist agent:
  - researcher: 知识库检索 + 联网搜索 + 记忆管理
  - coder:      代码执行 + 文件操作
  - analyst:    数据分析 + 表格生成 + 可视化

每个 specialist 有独立的工具子集和系统提示词后缀。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Specialist 定义
# ---------------------------------------------------------------------------


@dataclass
class Specialist:
    """专业子 Agent 的配置。"""

    name: str                               # 内部标识: "researcher" | "coder" | "analyst" | "general"
    display_name: str                       # 用户可读名称
    description: str                        # 能力描述（供 supervisor 决策用）
    tool_prefixes: list[str] = field(default_factory=list)  # 工具名前缀/完整名
    system_prompt_suffix: str = ""          # 追加到主 system prompt 的专属指令
    icon: str = ""                          # 前端展示图标


# ── 专家定义 ──────────────────────────────────────────────────────────────

RESEARCHER = Specialist(
    name="researcher",
    display_name="研究员",
    description="负责信息检索：搜索知识库、联网查询、管理用户记忆。适合查找文档、查询资料、检索历史信息。",
    tool_prefixes=[
        "tool_search_knowledge",
        "tool_list_knowledge_sources",
        "tool_index_knowledge",
        "tool_add_memory",
        "tool_search_memory",
        "tool_forget_memory",
        "tool_list_memories",
        "tool_save_episodic_memory",
        "tool_get_conversation_context",
        "mcp_searxng_",
    ],
    system_prompt_suffix=(
        "\n\n## 研究员模式\n"
        "你当前处于「研究员」角色，专注于信息检索和知识管理：\n"
        "- 优先使用知识库搜索和联网搜索获取信息\n"
        "- 用户提供的信息应保存到记忆系统中\n"
        "- 回答时引用来源，让用户知道信息的出处\n"
        "- 如果搜索不到足够信息，明确告知用户\n"
    ),
    icon="🔍",
)

CODER = Specialist(
    name="coder",
    display_name="代码专家",
    description="负责代码编写与执行、文件系统操作。适合编写脚本、调试代码、管理文件、执行自动化任务。",
    tool_prefixes=[
        "execute_code",
        "mcp_read_file",
        "mcp_write_file",
        "mcp_edit_file",
        "mcp_list_directory",
        "mcp_directory_tree",
        "mcp_search_files",
        "mcp_move_file",
        "mcp_get_file_info",
    ],
    system_prompt_suffix=(
        "\n\n## 代码专家模式\n"
        "你当前处于「代码专家」角色，专注于代码编写和文件操作：\n"
        "- 执行代码前先确认操作的安全性\n"
        "- 修改文件前先读取文件内容\n"
        "- 使用 Markdown 代码块展示代码片段\n"
        "- 解释关键代码逻辑，不只是贴代码\n"
        "- 错误信息应清晰传达给用户\n"
    ),
    icon="💻",
)

ANALYST = Specialist(
    name="analyst",
    display_name="数据分析师",
    description="负责数据分析、报表生成、图表绘制。适合分析 Excel/CSV 数据、统计汇总、生成可视化报告。",
    tool_prefixes=[
        "execute_code",
        "mcp_read_file",
        "mcp_write_file",
        "mcp_list_directory",
        "mcp_get_file_info",
        "tool_search_knowledge",
    ],
    system_prompt_suffix=(
        "\n\n## 数据分析师模式\n"
        "你当前处于「数据分析师」角色，专注于数据处理和可视化：\n"
        "- 使用 Python（pandas/matplotlib/openpyxl）处理数据\n"
        "- 先探查数据结构和规模，再确定分析方案\n"
        "- 分析结果用表格汇总，注明关键发现\n"
        "- 图表自动保存到文件，告知用户文件路径\n"
        "- 数据异常时主动提醒用户\n"
    ),
    icon="📊",
)

GENERAL = Specialist(
    name="general",
    display_name="通用助手",
    description="处理不适合委派给其他专家的任务：简单对话、多工具协同、RPA 操作、跨领域任务。",
    tool_prefixes=[],  # 空 = 全部工具
    system_prompt_suffix="",
    icon="🤖",
)

# ── 专家注册表 ──────────────────────────────────────────────────────────

SPECIALISTS: dict[str, Specialist] = {
    s.name: s for s in [RESEARCHER, CODER, ANALYST, GENERAL]
}

SPECIALISTS_LIST: list[Specialist] = [RESEARCHER, CODER, ANALYST, GENERAL]


# ---------------------------------------------------------------------------
# Supervisor 路由逻辑
# ---------------------------------------------------------------------------


SUPERVISOR_PROMPT = """你是一个任务协调器（Supervisor）。分析用户请求，决定委派给哪个专业 Agent。

## 可用专家
1. **研究员 (researcher)**: 知识库检索、联网搜索、记忆管理
   → 适合：查询文档、搜索资料、查找信息、记住偏好
2. **代码专家 (coder)**: 代码编写执行、文件系统操作
   → 适合：写代码、运行脚本、读写文件、自动化任务
3. **数据分析师 (analyst)**: 数据处理、统计、图表、报表
   → 适合：分析 Excel/CSV、统计汇总、生成图表、数据可视化
4. **通用助手 (general)**: 所有能力，适合多领域或无法归类
   → 适合：简单对话、跨领域任务、RPA 操作、复杂多步协调

## 决策规则
- 单一领域的请求 → 委派给对应 expert
- 跨领域请求（如 "分析数据然后搜索行业趋势"）→ 列出所有需要的专家
- 简单问候/确认/闲聊 → general
- 不确定时 → general

## 输出格式（严格 JSON）
{"specialist": "researcher|coder|analyst|general", "reason": "一句话委派理由", "sub_tasks": ["子任务1", "子任务2（可选）"]}

只输出 JSON，不要其他内容。"""


def match_specialist_tools(specialist: Specialist, all_tools_map: dict) -> list:
    """从工具库中筛选匹配 specialist 的工具。

    Args:
        specialist: Specialist 配置。
        all_tools_map: {tool_name: tool_object} 字典。

    Returns:
        匹配的工具列表（保持原始顺序）。
    """
    if not specialist.tool_prefixes:
        # 空列表 = 全部工具
        return list(all_tools_map.values())

    matched: list = []
    matched_names: set[str] = set()

    for prefix in specialist.tool_prefixes:
        for name, tool in all_tools_map.items():
            if name in matched_names:
                continue
            if name == prefix or name.startswith(prefix):
                matched.append(tool)
                matched_names.add(name)

    return matched


def get_specialist(specialist_name: str) -> Specialist | None:
    """根据名称获取 Specialist 配置。"""
    return SPECIALISTS.get(specialist_name)


# ---------------------------------------------------------------------------
# 多 Agent 消息协议 + mailbox（借鉴 Codex multi-agent 消息语义）
# ---------------------------------------------------------------------------
#
# Codex 的多 Agent 通过统一的消息协议（NEW_TASK / MESSAGE / FINAL_ANSWER）
# 在 supervisor 与 specialist 之间传递任务与结果。这里提供同构的轻量实现：
# - 消息统一为 (kind, task, sender, payload) 四元组，可安全序列化进
#   ``specialist_history`` / ``specialist_task`` 等 state 字段。
# - Mailbox 提供按任务汇聚消息的队列语义，supervisor 派发后从 mailbox 收结果。
# 与现有 supervisor 路由（SUPERVISOR_PROMPT + match_specialist_tools）正交，
# 仅统一「消息格式」这一层，不改动既有路由逻辑。

from dataclasses import dataclass, field as _field

# 消息类型常量
NEW_TASK = "new_task"
MESSAGE = "message"
FINAL_ANSWER = "final_answer"

# 合法消息类型集合
_MESSAGE_KINDS = {NEW_TASK, MESSAGE, FINAL_ANSWER}


@dataclass
class AgentMessage:
    """inter-agent 消息。"""
    kind: str                      # new_task | message | final_answer
    task: str                      # 任务名（关联一次委派）
    sender: str = ""               # 发送方 specialist 名
    payload: str = ""              # 消息内容 / 任务描述 / 最终答案

    def __post_init__(self) -> None:
        if self.kind not in _MESSAGE_KINDS:
            raise ValueError(f"未知消息类型: {self.kind!r}（应为 {sorted(_MESSAGE_KINDS)} 之一）")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "task": self.task, "sender": self.sender, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict) -> "AgentMessage":
        return cls(
            kind=data.get("kind", ""),
            task=data.get("task", ""),
            sender=data.get("sender", ""),
            payload=data.get("payload", ""),
        )


def encode_message(kind: str, task: str, sender: str = "", payload: str = "") -> dict:
    """把消息编码为可序列化字典（state 字段可安全存储）。"""
    return AgentMessage(kind=kind, task=task, sender=sender, payload=payload).to_dict()


def decode_message(data: dict) -> AgentMessage:
    """把字典解码为 AgentMessage（含类型校验）。"""
    return AgentMessage.from_dict(data)


@dataclass
class Mailbox:
    """按任务汇聚消息的 mailbox。

    supervisor 派发任务（NEW_TASK）后，specialist 的消息（MESSAGE /
    FINAL_ANSWER）按 ``task`` 分组入队；``collect(task)`` 取回该任务的全部消息。
    """
    messages: dict[str, list[AgentMessage]] = _field(default_factory=dict)

    def send(self, msg: AgentMessage) -> None:
        self.messages.setdefault(msg.task, []).append(msg)

    def collect(self, task: str) -> list[AgentMessage]:
        return list(self.messages.get(task, []))

    def final_answers(self, task: str) -> list[AgentMessage]:
        """取回某任务的最终答案（FINAl_ANSWER）。"""
        return [m for m in self.collect(task) if m.kind == FINAL_ANSWER]

    def all_tasks(self) -> list[str]:
        return list(self.messages.keys())
