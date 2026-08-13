"""提示词管理模块。

提供提示词模板的版本化加载、热重载、Jinja2 渲染功能。
与 eval 模块集成支持 A/B 对比评估。
"""

from .manager import PromptManager, get_prompt_manager, PromptTemplate

__all__ = [
    "PromptManager",
    "get_prompt_manager",
    "PromptTemplate",
]
