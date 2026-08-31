# -*- coding: utf-8 -*-
"""亚马逊轨迹跟踪表更新任务的契约：参数 schema + description。"""

from pydantic import BaseModel, Field


class TrackTableArgs(BaseModel):
    store: str = Field(
        default="",
        description="店铺标签（如 '巧逗豆'/'天安'）。为空则处理 TRACK_TABLE_STORES 中全部店铺。",
    )


DESCRIPTION = (
    "更新亚马逊轨迹跟踪表：从领星ERP下载最新FBA库存 → 紫鸟开店铺 → "
    "爬取各货件实际接收/状态 → 计算校正后接收量与售卖批次 → 写回跟踪表Excel。"
    "真实业务操作（会修改共享盘跟踪表），需审批。"
)
