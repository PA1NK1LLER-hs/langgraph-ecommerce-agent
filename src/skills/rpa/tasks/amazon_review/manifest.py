# -*- coding: utf-8 -*-
"""Amazon 评论/星级采集的契约：参数 schema + description。"""

from pydantic import BaseModel, Field


class AmazonReviewArgs(BaseModel):
    excel_path: str = Field(
        default="",
        description="ASIN Excel 文件路径。为空则用 .env 的 AMAZON_REVIEW_EXCEL_PATH。",
    )


DESCRIPTION = (
    "采集 Amazon 商品评论数和星级。从 Excel 读取 ASIN 列表，多浏览器并发访问 Amazon 各站点，"
    "采集每个商品的评分（star）与评论数（review），失败自动重试，按国家分组返回结果。"
)
