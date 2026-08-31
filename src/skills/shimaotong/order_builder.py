# -*- coding: utf-8 -*-
"""
订单构建模块 - 世贸通抬头报关。

负责从 Excel 文件（明细6 sheet）读取数据，结合 SKU API 返回值，
构建符合世贸通抬头报关接口格式的完整订单 dict。

数据来源：
  ┌────────────┬──────────────────────┬─────────────────────┐
  │ 来源        │ 字段                  │ 说明                 │
  ├────────────┼──────────────────────┼─────────────────────┤
  │ Excel 列名  │ 编号/预估海运费/保险费│ 订单级元数据          │
  │ Excel 列名  │ 华飞系统型号SKU        │ 查询键               │
  │ Excel 列名  │ 合计数量              │ packingQuantity      │
  │ Excel 列名  │ 单价                  │ purUnitPrice         │
  │ Excel 列名  │ 报关出口单价           │ unitPrice            │
  │ Excel 列名  │ 报关出口价格合计        │ totalAmount          │
  │ SKU API     │ hsCode/chName/尺寸等   │ 系统自动填充           │
  │ config.py   │ 联系人/贸易方式等      │ 固定默认值             │
  └────────────┴──────────────────────┴─────────────────────┘
"""
import pandas as pd
import requests

from .config import (
    get_order_defaults,
    FFILL_COL_NAMES,
    COL_MAP,
    translate,
    resolve_column_names,
)
from .api_client import fetch_sku_info


# ---- 模块级缓存：缓存列名映射，切换文件时自动刷新 ----
_cached_excel_path = None
_cached_df = None
_cached_cols = {}   # {逻辑名: 实际列名}
_cached_ffill_cols = []  # 实际的 ffill 列名列表


def _load_excel_with_header(excel_path: str) -> pd.DataFrame:
    """读取 Excel 并返回带列名的 DataFrame，切换文件时自动刷新缓存。"""
    global _cached_excel_path, _cached_df, _cached_cols, _cached_ffill_cols

    # 路径变了 → 清空缓存重新加载
    if _cached_excel_path != excel_path:
        _cached_excel_path = excel_path
        _cached_df = None
        _cached_cols = {}
        _cached_ffill_cols = []

    if _cached_df is not None:
        return _cached_df

    df = pd.read_excel(excel_path, sheet_name=2, header=0)
    _cached_df = df
    _cached_cols = resolve_column_names(list(df.columns))

    # 解析实际的 ffill 列名
    _cached_ffill_cols = []
    for expected in FFILL_COL_NAMES:
        for col in df.columns:
            if expected in str(col):
                _cached_ffill_cols.append(col)
                break

    return df


def _col(name: str) -> str:
    """获取逻辑名对应的实际列名。"""
    if name not in _cached_cols:
        raise KeyError(f"列 '{COL_MAP.get(name, name)}' 未在 Excel 中找到，已映射列: {list(_cached_cols.values())}")
    return _cached_cols[name]


def detect_orders(excel_path: str) -> list[str]:
    """
    从 Excel 中自动检测所有子订单号（按列名读取）。

    Args:
        excel_path: Excel 文件路径

    Returns:
        排序后的子订单号列表
    """
    df = _load_excel_with_header(excel_path)
    po_col = _col("order_no")

    # 提取所有含 "PO" 且长度 > 12 的唯一值
    po_series = df[po_col].dropna().astype(str)
    orders = sorted(set(
        o for o in po_series if "PO" in o and len(o) > 12
    ))
    return orders


def build_order(
    session: requests.Session,
    excel_path: str,
    po_order_no: str,
    new_order_no: str,
) -> dict:
    """
    从 Excel 构建单个子订单的完整 payload（按列名读取）。

    Args:
        session:      已登录的 requests.Session
        excel_path:   Excel 文件路径
        po_order_no:  PO 子订单号，用作 Excel 搜索键
        new_order_no: 新的系统订单号

    Returns:
        完整订单 dict，可直接传给 save_order()
    """
    df = _load_and_prepare(excel_path, po_order_no)

    # ---- 订单级字段（按列名读取，中文转英文） ----
    first = df.iloc[0]
    consignee = _safe_val(first, "warehouse_addr")
    freight = _safe_val(first, "freight", "0")
    ins_fee = _safe_val(first, "ins_fee", "0")
    ins_amount = _safe_val(first, "ins_amount", "0")
    warehouse_no = _safe_val(first, "warehouse_no")
    remark = f"进仓单号:{warehouse_no}" if warehouse_no else ""

    from_port = translate(_safe_val(first, "origin_port", "上海"))
    dest_port = translate(_safe_val(first, "dest_port", "纽约"))
    country_cn = _safe_val(first, "country", "美国")
    country_en = translate(country_cn)

    # ---- SKU 明细 ----
    sku_groups = _aggregate_skus(df, session)
    details = _build_details(sku_groups, session)

    # ---- 组装 ----
    order = {
        **get_order_defaults(),
        "orderNo": new_order_no,
        "contractNo": po_order_no,
        "consignee": consignee,
        "freight": freight,
        "insFee": ins_fee,
        "insAmount": ins_amount,
        "fromPort": from_port,
        "destPort": dest_port,
        "destCountry": country_en,
        "tradeCountry": country_en,
        "remark": remark,
        "orderDetailDtoList": details,
    }
    return order


# ---- 内部函数 ----

def _load_and_prepare(excel_path: str, po_order_no: str) -> pd.DataFrame:
    """加载 Excel 并筛选出指定子订单的行，对合并单元格列做 forward-fill。"""
    if _cached_df is not None:
        df = _cached_df.copy()
    else:
        df = pd.read_excel(excel_path, sheet_name=2, header=0)

    po_col = _col("order_no")

    # forward-fill 合并单元格列
    for col in _cached_ffill_cols:
        if col in df.columns:
            df[col] = df[col].ffill()

    # 筛选子订单行（只保留订单头开始的连续行）
    po_mask = df[po_col].astype(str).str.contains(po_order_no, na=False)
    if not po_mask.any():
        raise ValueError(f"未找到子订单: {po_order_no}")

    # 找到该订单第一次出现的位置
    start_idx = po_mask.idxmax()
    # 取从 start_idx 到下一个订单头（或到末尾）
    subset = df.iloc[start_idx:].reset_index(drop=True)

    # 剔除后续其他订单的行（通过"合计"行或下一个PO行判断）
    cut_idx = None
    for i in range(1, len(subset)):
        val = str(subset.loc[i, po_col])
        if "PO" in val and po_order_no not in val:
            cut_idx = i
            break
        if "合计" in val:
            cut_idx = i
            break

    if cut_idx is not None:
        subset = subset.iloc[:cut_idx]

    # 过滤掉空的明细行（华飞SKU 为空）
    sku_col = _col("huafei_sku")
    subset = subset[subset[sku_col].notna()].reset_index(drop=True)

    if subset.empty:
        raise ValueError(f"子订单 {po_order_no} 无有效明细行")

    return subset


def _safe_int(val, default: int = 0) -> int:
    """安全转整数，NaN、空、非数字全部返回默认值"""
    try:
        f = float(val)
        if f != f:  # NaN 检测（NaN != NaN）
            return default
        return int(f)
    except (ValueError, TypeError):
        return default


def _is_numeric(s: str) -> bool:
    """判断字符串是否为纯数字（整数或浮点数），如 '123'、'45.6'。"""
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _safe_float(val, default: float = 0.0) -> float:
    """尝试将值转为 float，NaN、空、非数字全部返回 default。"""
    try:
        f = float(val)
        if f != f:  # NaN 检测
            return default
        return f
    except (ValueError, TypeError):
        return default


def _safe_val(row: pd.Series, logical_name: str, default: str = "") -> str:
    """按列名安全取值，NaN 时返回默认值。"""
    col = _col(logical_name)
    val = row[col]
    return str(val) if pd.notna(val) else default


def _aggregate_skus(df: pd.DataFrame, session: requests.Session) -> dict:
    """
    按华飞系统型号SKU 聚合数量和单价。

    Returns:
        {sku: {"qty": int, "unit_price": float, "pur_price": float}, ...}
    """
    sku_col = _col("huafei_sku")
    qty_col = _col("total_qty")
    up_col = _col("unit_price_export")
    pur_col = _col("unit_price_pur")

    groups: dict[str, dict] = {}

    for _, row in df.iterrows():
        sku = _safe_val(row, "huafei_sku")
        # 跳过空值、NaN、纯数字（Excel 可能把空单元格读成 0 或数字）
        if not sku or sku.lower() == "nan" or _is_numeric(sku):
            continue
        qty = _safe_int(_safe_float(row[qty_col]))
        if qty == 0:
            continue

        unit_price = round(_safe_float(row[up_col]), 2)
        if unit_price == 0:
            continue

        pur_price = round(_safe_float(row[pur_col]), 2)

        if sku not in groups:
            groups[sku] = {"qty": 0, "unit_price": unit_price, "pur_price": pur_price}
        groups[sku]["qty"] += qty

    return groups


def _build_details(sku_groups: dict, session: requests.Session) -> list[dict]:
    """为每个去重后的 SKU 构建 orderDetailDtoList 条目，直接使用 autocomplete API 返回值。"""
    details = []

    for idx, (sku, agg) in enumerate(sku_groups.items()):
        qty = agg["qty"]
        unit_price = agg["unit_price"]
        pur_price = agg["pur_price"]
        total_amt = round(unit_price * qty, 2)

        # 调 SKU API 获取商品填充数据，直接作为 detail 基础结构
        info = fetch_sku_info(session, sku)
        if not info:
            info = {}

        ug = _safe_float(info.get("unitGross", 0))
        un = _safe_float(info.get("unitNet", 0))
        length = _safe_float(info.get("length", 0))
        width = _safe_float(info.get("width", 0))
        height = _safe_float(info.get("height", 0))

        # 以 API 返回值为基础，追加 Excel 数据和固定字段
        detail = {
            **info,
            "supplierName": info.get("unitName", ""),
            "taxRate": str(info.get("tsl", "")),
            # Excel 覆盖字段
            "sku": sku,
            "packingQuantity": str(qty),

            "totalQuantity": str(qty),
            "unitPrice": str(unit_price),
            "totalAmount": str(total_amt),
            "purUnitPrice": str(pur_price),
            "purTotalAmount": str(round(pur_price * qty, 2)),
            "volume": str(round(length * width * height / 1e6 * qty, 3)),
            "unitGross": str(ug),
            "totalGross": str(round(ug * qty, 2)),
            "unitNet": str(un),
            "totalNet": str(round(un * qty, 2)),
            # 固定值
            "parentId": "",
            "addGoods": "0",
            "origin": "宿迁",
            "mark": "",
            "remark": "",
            "index": idx + 1,
        }
        details.append(detail)

    return details
