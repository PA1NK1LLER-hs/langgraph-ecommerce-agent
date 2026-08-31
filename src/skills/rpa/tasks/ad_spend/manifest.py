# -*- coding: utf-8 -*-
"""广告花费查询的契约：参数 schema + description。"""

from pydantic import BaseModel, Field


class AdSpendArgs(BaseModel):
    start_date: str = Field(description="起始日期，格式 YYYY-MM-DD，如 2026-06-01")
    end_date: str = Field(
        default="",
        description="结束日期，格式 YYYY-MM-DD。为空则只查 start_date 当天。",
    )
    output_dir: str = Field(
        default="",
        description="广告花费报表输出目录。为空则用 .env 的 AD_SPEND_OUTPUT_DIR。",
    )


DESCRIPTION = (
    "查询各站点产品日广告花费，支持多日期范围 + 断点续写。"
    "全流程自动完成：启动紫鸟→开店铺→IP 检测→按国家/日期筛选→逐商品查询花费→写回月度 Excel。"
    "中断后重跑会自动跳过已完成的日期/商品（断点）。"
)
