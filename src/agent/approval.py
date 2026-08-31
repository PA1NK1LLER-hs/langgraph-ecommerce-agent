"""Human-in-the-Loop 审批模块。

定义哪些工具调用需要人工审批、风险等级分类、审批消息构建。
所有函数为纯函数，无副作用，便于单元测试。
"""

from typing import Literal

RiskLevel = Literal["low", "medium", "high"]
ApprovalMode = Literal["strict", "standard", "off"]

# ── 高风险工具注册表 ──
# (工具名前缀, 风险等级, 审批原因描述)
HIGH_RISK_TOOLS: list[tuple[str, RiskLevel, str]] = [
    ("execute_code", "high", "执行代码（可能修改文件系统或运行任意命令）"),
    ("mcp_rpa_", "high", "浏览器自动化操作（可能影响生产环境店铺数据）"),
    ("rpa_", "high", "浏览器自动化操作（可能影响生产环境店铺数据）"),
    ("submit_rpa_", "high", "提交 RPA 批量任务（执行后可能影响生产环境店铺数据）"),
    ("amazon_", "high", "Amazon 店铺操作（可能影响实际业务）"),
    ("mcp_write_file", "medium", "写入文件"),
    ("mcp_edit_file", "medium", "编辑文件内容"),
    ("mcp_move_file", "medium", "移动/重命名文件"),
    ("mcp_delete_files", "high", "删除文件（不可逆操作）"),
    ("mcp_search_files", "low", "搜索文件（只读，自动批准）"),  # 显式标记 low 以便文档化
    ("tool_forget_memory", "medium", "删除用户记忆数据"),
    ("tool_index_knowledge", "medium", "将内容写入知识库"),
    ("mcp_docker_", "high", "Docker 容器管理（可能影响运行中的服务）"),
    ("tool_shimaotong_", "high", "世贸通抬头报关（可能真实创建/提交业务订单）"),
]

# risk_level → emoji 展示映射
RISK_EMOJI: dict[str, str] = {
    "low": "ℹ️",     # ℹ️
    "medium": "⚠️",  # ⚠️
    "high": "\U0001f534",      # 🔴
}


def classify_tool_risk(
    tool_name: str,
    mode: ApprovalMode = "standard",
) -> tuple[bool, RiskLevel, str]:
    """检查工具调用是否需要人工审批。

    Args:
        tool_name: 工具名称（如 "execute_code", "rpa_start_browser"）
        mode: 审批模式
            - "off": 关闭所有审批，全部自动执行
            - "standard": 仅高风险工具需要审批
            - "strict": 除显式标记为 low 的工具外都需要审批

    Returns:
        (needs_approval, risk_level, reason)
        - needs_approval: 是否需要审批
        - risk_level: 风险等级
        - reason: 审批原因描述（不需要审批时为空字符串）
    """
    if mode == "off":
        return False, "low", ""

    if mode == "strict":
        # 严格模式：只有显式标记为 low 的放行
        for prefix, level, reason in HIGH_RISK_TOOLS:
            if tool_name.startswith(prefix):
                if level == "low":
                    return False, "low", ""
                return True, level, reason
        # 不在注册表中的工具 → 严格模式默认需要审批
        return True, "medium", f"未知工具 '{tool_name}'（严格模式需要审批）"

    # standard 模式：只有高风险工具需要审批
    for prefix, level, reason in HIGH_RISK_TOOLS:
        if tool_name.startswith(prefix):
            if level == "low":
                return False, "low", ""
            return True, level, reason

    # 不在注册表中的工具 → 低风险，自动执行
    return False, "low", ""


def build_approval_message(
    tool_name: str,
    tool_args: dict | None = None,
) -> str:
    """构建给用户看的审批消息（Markdown 格式）。

    Args:
        tool_name: 工具名称
        tool_args: 工具调用参数

    Returns:
        格式化的 Markdown 审批提示文本
    """
    needs, level, reason = classify_tool_risk(tool_name)
    if not needs:
        return ""

    emoji = RISK_EMOJI.get(level, "❓")  # ❓ fallback
    args_str = str(tool_args)[:300] if tool_args else "(无参数)"

    return (
        f"{emoji} **需要确认：{reason}**\n\n"
        f"- 工具: `{tool_name}`\n"
        f"- 风险等级: **{level.upper()}**\n"
        f"- 参数: `{args_str}`"
    )


def build_approval_payload(
    tool_calls: list[dict],
    reason_override: str = "",
) -> dict:
    """构建发送给前端的审批请求 payload。

    Args:
        tool_calls: 需要审批的工具调用列表，每个元素含 id/name/args
        reason_override: 自定义审批原因（为空则自动生成）

    Returns:
        {
            "type": "approval_required",
            "calls": [{id, name, args, risk_level, reason}],
            "message": "Markdown 格式的审批提示",
            "total_risky": N,
        }
    """
    calls = []
    for tc in tool_calls:
        name = tc.get("name", "")
        needs, level, reason = classify_tool_risk(name)
        calls.append({
            "id": tc.get("id", ""),
            "name": name,
            "args": tc.get("args", {}),
            "risk_level": level,
            "reason": reason_override or reason,
        })

    # 构建汇总消息
    high_count = sum(1 for c in calls if c["risk_level"] == "high")
    medium_count = sum(1 for c in calls if c["risk_level"] == "medium")
    parts = []
    if high_count:
        parts.append(f"{high_count} 个高风险操作")
    if medium_count:
        parts.append(f"{medium_count} 个中风险操作")
    summary = "、".join(parts) if parts else f"{len(calls)} 个操作"

    messages = []
    for c in calls:
        msg = build_approval_message(c["name"], c.get("args"))
        if msg:
            messages.append(msg)

    return {
        "type": "approval_required",
        "calls": calls,
        "message": f"## 待确认：{summary}\n\n" + "\n\n---\n\n".join(messages),
        "total_risky": len(calls),
    }


def get_approval_mode() -> ApprovalMode:
    """从环境变量读取审批模式。"""
    import os
    mode = os.getenv("APPROVAL_MODE", "standard").lower()
    if mode in ("off", "strict", "standard"):
        return mode  # type: ignore[return-value]
    return "standard"


def check_command_policy(tool_args: dict | None) -> tuple[bool, str]:
    """对工具参数中的 shell/代码命令做命令级策略检查（prefix_rule 引擎）。

    作为工具名粒度审批之外的第二道闸：即使 ``execute_code`` 已是 high 风险，
    这里也能把「包含明确破坏性命令」的调用单点升级为 forbidden。

    从 args 中识别命令文本（code / command / cmd / script 字段），
    命中 forbidden 规则时返回 (True, 原因)；否则返回 (False, "")。

    Returns:
        (is_forbidden, reason)
    """
    if not isinstance(tool_args, dict):
        return False, ""

    from .exec_policy import Decision, check_command

    for key in ("code", "command", "cmd", "script", "text"):
        raw = tool_args.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        # 多行代码/脚本逐行检查，任一行 forbidden 即整体 forbidden
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if check_command(line) >= Decision.FORBIDDEN:
                return True, f"命令被安全策略禁止：`{line[:120]}`"
    return False, ""
