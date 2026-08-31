"""技能系统 — 基于 @tool 装饰器的插件化工具注册。

所有技能均使用 @tool 装饰器直接定义在各自的模块中。
get_skill_tools() 收集所有技能工具供 Agent 绑定。

RPA 批量任务（紫鸟浏览器）不在此注册——始终由独立 RPA MCP 进程提供
（mcp_rpa_*，本机 stdio / 跨机 HTTP URL），agent 进程内不跑 RPA，
见 agent.mcp_setup 的 setup_rpa_mcp / ensure_mcp_for_intent。
"""

import logging

from app_context import get_app_context

logger = logging.getLogger("skills")


def _discover_tools() -> list:
    """容错发现所有技能工具。单个模块加载失败不影响其他模块。"""
    tools: list = []

    # code_executor — 代码执行
    try:
        from skills.code_executor import execute_code
        tools.append(execute_code)
    except ImportError as e:
        logger.warning("Skill code_executor unavailable: %s", e)

    # shimaotong — 世贸通抬头报关（HTTP API 自动化，独立于浏览器 RPA）
    try:
        from skills.shimaotong import get_shimaotong_tools
        tools.extend(get_shimaotong_tools())
    except ImportError as e:
        logger.warning("Skill shimaotong unavailable: %s", e)

    return tools


def get_skill_tools() -> list:
    """返回所有技能 @tool 函数列表（带缓存）。"""
    ctx = get_app_context()
    cached = ctx.get_skill_tools_cache()
    if cached is None:
        cached = _discover_tools()
        ctx.set_skill_tools_cache(cached)
    return cached


def list_skills() -> list[dict]:
    """列出所有已注册的技能（供 CLI /skills 命令使用）。"""
    tools = get_skill_tools()
    return [
        {"name": t.name, "description": (t.description or "").split("\n")[0][:200]}
        for t in tools
    ]
