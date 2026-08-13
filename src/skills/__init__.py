"""技能系统 — 基于 @tool 装饰器的插件化工具注册。

所有技能均使用 @tool 装饰器直接定义在各自的模块中。
get_skill_tools() 收集所有技能工具供 Agent 绑定。
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

    # rpa_ziniao — 紫鸟浏览器 RPA（15 个工具）
    try:
        from skills.rpa_ziniao import get_rpa_tools
        tools.extend(get_rpa_tools())
    except ImportError as e:
        logger.warning("Skill rpa_ziniao unavailable: %s", e)

    # rpa_amazon_get_review — Amazon 评论采集
    try:
        from skills.rpa_amazon_get_review import amazon_get_review
        tools.append(amazon_get_review)
    except ImportError as e:
        logger.warning("Skill rpa_amazon_get_review unavailable: %s", e)

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
