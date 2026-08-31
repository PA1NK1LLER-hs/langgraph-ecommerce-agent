# -*- coding: utf-8 -*-
"""RPA 技能包 — 统一入口。

结构：
  common/      紫鸟客户端 + Excel IO + 断点（全项目唯一实现）
  tasks/         批量端到端任务（每个任务一个目录，run(payload) 契约）
  adapters.py    把 task 包装成 @tool，并提供 get_rpa_tools()
"""

from skills.rpa.adapters import get_rpa_tools, get_rpa_batch_tools

__all__ = ["get_rpa_tools", "get_rpa_batch_tools"]
