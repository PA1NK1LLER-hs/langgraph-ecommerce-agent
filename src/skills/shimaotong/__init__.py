# -*- coding: utf-8 -*-
"""世贸通抬头报关技能 — 从报关 Excel 检测子订单、登录世贸通、构建保存订单、可选提交报关资料。

HTTP API 自动化（requests），不依赖浏览器/RPA。重依赖（ddddocr/opencv）惰性导入，
agent 环境未装也不影响 skill 加载，仅在登录真正调用时给出安装指引。
"""

from langchain_core.tools import tool

from skills.shimaotong.flow import run as shimaotong_run
from skills.shimaotong.manifest import ShimaotongArgs, DESCRIPTION


@tool(args_schema=ShimaotongArgs, description=DESCRIPTION)
def tool_shimaotong_submit(excel_path: str = "", submit_customs: bool = False,
                           target_orders: str = "") -> dict:
    """世贸通抬头报关：登录 → 检测子订单 → 构建保存 → 可选提交报关资料。

    excel_path 留空时自动发现网络共享跟踪表中的待办报关 Excel 并批量处理。
    """
    return shimaotong_run({
        "excel_path": excel_path,
        "submit_customs": submit_customs,
        "target_orders": target_orders,
    })


def get_shimaotong_tools() -> list:
    """返回世贸通抬头报关的所有 @tool。"""
    return [tool_shimaotong_submit]
