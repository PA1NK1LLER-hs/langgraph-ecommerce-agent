# -*- coding: utf-8 -*-
"""RPA 批量任务适配器 — 把每个 task 的 run() 包装成 @tool。

Agent 只暴露 3 个固定流程批量任务，不暴露逐步操作浏览器的原子工具
（RPA 一定是固定流程，不做交互式逐步操作）。这些工具始终由独立 RPA MCP
进程执行（本机 stdio / 跨机 HTTP，见 agent.mcp_setup），agent 进程内不
直接调用；本模块同时供 RPA MCP server（mcp_server.py）注册。
执行日志写 RPA 进程日志，实时回流为后续 Phase 2（跨进程日志转发）。
"""

from langchain_core.tools import tool

from skills.rpa.tasks.ad_spend.flow import run as ad_spend_run
from skills.rpa.tasks.ad_spend.manifest import AdSpendArgs, DESCRIPTION as AD_SPEND_DESC
from skills.rpa.tasks.amazon_review.flow import run as amazon_review_run
from skills.rpa.tasks.amazon_review.manifest import AmazonReviewArgs, DESCRIPTION as AMAZON_REVIEW_DESC
from skills.rpa.tasks.track_table.flow import run as track_table_run
from skills.rpa.tasks.track_table.manifest import TrackTableArgs, DESCRIPTION as TRACK_TABLE_DESC


@tool(args_schema=AdSpendArgs, description=AD_SPEND_DESC)
def rpa_query_campaign_spend(start_date: str, end_date: str = "", output_dir: str = "") -> dict:
    """查询各站点产品日广告花费（多日期 + 断点续写）。"""
    return ad_spend_run({"start_date": start_date, "end_date": end_date, "output_dir": output_dir})


@tool(args_schema=AmazonReviewArgs, description=AMAZON_REVIEW_DESC)
def rpa_collect_amazon_review(excel_path: str = "") -> dict:
    """采集 Amazon 商品评论数和星级。"""
    return amazon_review_run({"excel_path": excel_path})


@tool(args_schema=TrackTableArgs, description=TRACK_TABLE_DESC)
def rpa_update_track_table(store: str = "") -> dict:
    """更新亚马逊轨迹跟踪表（领星下载库存 → 紫鸟爬取 → 写回跟踪表）。"""
    return track_table_run({"store": store})


def get_rpa_batch_tools() -> list:
    """批量端到端任务（RPA 的全部工具，供独立 RPA MCP server 注册）。

    RPA 始终由独立 MCP 进程执行（本机 stdio / 跨机 HTTP，见 agent.mcp_setup），
    agent 进程内不直接调用。执行日志写 RPA 进程日志，实时回流为后续 Phase 2。
    """
    return [rpa_query_campaign_spend, rpa_collect_amazon_review, rpa_update_track_table]


def get_rpa_tools() -> list:
    """返回所有 RPA @tool（即 3 个批量任务）。"""
    return get_rpa_batch_tools()
