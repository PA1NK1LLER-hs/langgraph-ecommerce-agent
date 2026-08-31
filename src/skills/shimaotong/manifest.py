# -*- coding: utf-8 -*-
"""世贸通抬头报关的契约：参数 schema + description。"""

from pydantic import BaseModel, Field


class ShimaotongArgs(BaseModel):
    excel_path: str = Field(
        default="",
        description=(
            "报关 Excel 文件路径（明细6 sheet，含子订单与 SKU 明细数据）。"
            "留空则自动发现：从网络共享跟踪表（SHIMAOTONG_TRACKING_BASE）读取待办报关合同，"
            "逐个目录定位 Excel 并批量处理；单文件模式传显式路径即可。"
        ),
    )
    submit_customs: bool = Field(
        default=False,
        description="是否提交报关资料。默认 False 只保存订单；True 则对保存成功的订单自动提交报关资料（真实业务动作，需审批）。",
    )
    target_orders: str = Field(
        default="",
        description="指定要处理的子订单号（PO 号，逗号分隔，如 'PO260707003-3,PO260707003-4'）。为空则处理 Excel 中全部子订单。",
    )


DESCRIPTION = (
    "世贸通抬头报关：检测报关 Excel 中的子订单，登录世贸通系统，自动生成订单号并构建订单保存，可选提交报关资料。"
    "excel_path 留空时自动发现网络共享跟踪表（SHIMAOTONG_TRACKING_BASE）中的待办报关 Excel 并批量处理（登录一次、订单号序号跨文件递增），"
    "不传路径即可触发批量；传显式 excel_path 则单文件处理。"
    "全程自动处理订单号冲突重试；提交报关资料为真实业务操作，默认不执行，需显式指定并经过审批。"
)
