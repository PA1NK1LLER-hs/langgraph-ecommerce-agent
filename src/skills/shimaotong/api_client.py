# -*- coding: utf-8 -*-
"""
API 客户端模块 - 世贸通抬头报关。

提供：
  - SKU 自动填充查询
  - 订单列表查询 → 自动生成下一个 orderNo
  - 订单保存
  - 报关资料提交
"""

import os
import re
import time
import logging

import requests

from .config import (
    BASE_URL, HEADERS,
    ENDPOINT_SKU_AUTOCOMPLETE,
    ENDPOINT_ORDER_LIST,
    ENDPOINT_ORDER_SAVE,
    ENDPOINT_DOC_ORDER_LIST,
    ENDPOINT_ORDER_GET_INFO,
    ENDPOINT_ORDER_SUBMIT_CHECK,
    ENDPOINT_ORDER_SUBMIT,
    ORDER_LIST_PARAMS,
    ORDER_NO_PREFIX,
    get_order_defaults,
)

logger = logging.getLogger(__name__)


def fetch_sku_info(session: requests.Session, sku: str, max_retries: int = 3) -> dict:
    """调用 SKU 自动填充接口获取商品信息，空值时自动重试。"""
    base_sku = sku
    url = f"{BASE_URL}{ENDPOINT_SKU_AUTOCOMPLETE}?q={base_sku}"

    for attempt in range(max_retries):
        resp = session.get(url, headers={**HEADERS, "Referer": f"{BASE_URL}/"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("value", [])
        if isinstance(items, list) and items:
            return items[0]
        if attempt < max_retries - 1:
            time.sleep(0.5)
            logger.debug("SKU=%s 第%d次返回空，重试", sku, attempt + 1)

    logger.warning("SKU=%s 重试%d次仍为空", sku, max_retries)
    return {}


def find_order_folders() -> list:
    """
    从跟踪表"未做"sheet 获取操作人和合同编号，在对应"已进仓-{操作人}"文件夹中搜索合同编号。

    网络共享根路径从环境变量 `SHIMAOTONG_TRACKING_BASE` 读取；
    未配置或不可达时返回空列表（走显式 excel_path 的调用不受影响）。

    Returns:
        匹配的文件夹路径列表，未找到任何返回空列表
    """
    base = os.getenv("SHIMAOTONG_TRACKING_BASE", "")
    if not base:
        return []

    # 防御：.env 手滑写成单反斜杠（\192.168...）时自动补成 UNC 双反斜杠（\\192.168...）
    # 否则 os.path.isdir 会把单反斜杠解析为当前盘根相对路径，永远 False
    if base.startswith("\\") and not base.startswith("\\\\"):
        base = "\\" + base

    tracking_excel = os.path.join(base, "待做报关文件的出运合同号记录表.xlsx")

    if not os.path.isdir(base):
        return []

    entries = _load_tracking_table(tracking_excel)
    if not entries:
        return []

    path_list = []
    for operator, contract_no in entries:
        target = os.path.join(base, f"已进仓-{operator}")
        if not os.path.isdir(target):
            continue

        found = _search_one(target, contract_no)
        if found:
            path_list.append(found)

    return path_list


def _load_tracking_table(tracking_excel: str) -> list:
    """
    读取跟踪表"未做"sheet，返回 [(操作人, 合同编号), ...] 列表。

    Args:
        tracking_excel: 跟踪表 Excel 路径

    Returns:
        操作人和合同编号的配对列表，读取失败返回空列表
    """
    import pandas as pd
    try:
        df = pd.read_excel(tracking_excel, sheet_name="未做")
    except Exception:
        return []

    col_operator = col_contract = None
    for col in df.columns:
        col_str = str(col)
        if "操作人" in col_str:
            col_operator = col
        elif "合同编号" in col_str or "合同" in col_str:
            col_contract = col

    if col_operator is None or col_contract is None:
        return []

    result = []
    for _, row in df.iterrows():
        op = str(row[col_operator]).strip()
        cn = str(row[col_contract]).strip()
        if op and cn and op != "nan" and cn != "nan":
            result.append((op, cn))

    return result


def _search_one(root_dir: str, keyword: str) -> str | None:
    """在 root_dir 下搜索目录名含 keyword 的目录，找到一个立即返回。"""
    for dirpath, dirnames, _ in os.walk(root_dir):
        for d in dirnames:
            if keyword in d:
                return os.path.join(dirpath, d)
    return None


def fetch_next_order_no(session: requests.Session, page_size: int = 100) -> str:
    """
    查询订单列表（翻页拉全量），提取所有 WT26SHUY 开头的订单号，取最大数字 + 1。

    注意：不能复用 ORDER_LIST_PARAMS（含 searchType=1，会把列表查成 0 条）。
    分页拉取直到覆盖 total，避免只信最新一页而漏掉更大的号。

    Args:
        session: 已登录的 requests.Session
        page_size: 每页条数（默认 100）

    Returns:
        新的订单号，如 "WT26SHUY0215"
    """
    url = f"{BASE_URL}{ENDPOINT_ORDER_LIST}"
    pattern = re.compile(rf"{re.escape(ORDER_NO_PREFIX)}(\d+)")
    max_num = 0
    page = 1
    total = None

    while True:
        params = {
            "pageSize": str(page_size),
            "pageNum": str(page),
            "orderByColumn": "createTime",
            "isAsc": "desc",
            "searchType": "",  # 关键：searchType=1 会返回 0 条
            "orderNo": "", "contractNo": "", "consignee": "", "ldNo": "",
            "dcoeStatus": "", "customsNoStatus": "", "balanceStatus": "",
            "supplierName": "", "hsCode": "", "goodsName": "",
            "auditBeginTime": "", "auditEndTime": "", "beginTime": "", "endTime": "",
        }
        resp = session.post(url, data=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("rows", [])
        if total is None:
            total = int(data.get("total") or 0)

        for row in rows:
            order_no = str(row.get("orderNo", ""))
            m = pattern.search(order_no)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num

        # 翻页终止：当前页没拉满 / 已覆盖 total / total 未知但本页为空
        if not rows or len(rows) < page_size or page * page_size >= total:
            break
        page += 1

    next_num = max_num + 1
    new_order_no = f"{ORDER_NO_PREFIX}{next_num:04d}"
    logger.info("当前最大订单号 %s%04d → 新订单号 %s", ORDER_NO_PREFIX, max_num, new_order_no)
    return new_order_no


def save_order(session: requests.Session, order: dict) -> dict:
    """提交订单到世贸通（抬头报关接口）。"""
    url = f"{BASE_URL}{ENDPOINT_ORDER_SAVE}"
    resp = session.post(url, json=order,
                        headers={**HEADERS, "Content-Type": "application/json"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ==================== 报关资料提交（查 id → get/info → submit/check → submit） ====================

def query_order_id(session: requests.Session, order_no: str) -> str | None:
    """根据订单号查询系统内部 id（文档接口）。"""
    url = f"{BASE_URL}{ENDPOINT_DOC_ORDER_LIST}"
    resp = session.post(url, data={
        "pageSize": "10", "pageNum": "1",
        "orderByColumn": "createTime", "isAsc": "desc",
        "orderNo": order_no, "contractNo": "",
        "tradeCountry": "", "fromPort": "", "destPort": "",
        "minAmount": "", "maxAmount": "",
        "shipmentBeginDate": "", "shipmentEndDate": "",
        "beginTime": "", "endTime": "",
    }, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("rows", [])
    if rows and len(rows) > 0:
        return str(rows[0].get("id", ""))
    return None


def get_order_info(session: requests.Session, order_id: str) -> dict | None:
    """
    根据 order_id 获取订单详细信息。

    POST /api/business/order/get/info
    请求: {"id": order_id}（form 编码）
    返回: orderMain 订单详情 dict（已补全 userId），失败返回 None
    """
    url = f"{BASE_URL}{ENDPOINT_ORDER_GET_INFO}"
    resp = session.post(url, data={"id": order_id}, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        logger.warning("获取订单信息失败: %s", data.get("msg", data))
        return None
    # 响应结构: {"msg": "...", "code": 200, "orderMain": {...}}
    order_main = data.get("orderMain") or data.get("data") or {}

    # submit/check 和 submit 端点要求 userId 非空；
    # get/info 返回的 orderMain.userId 为 None，需用 createBy 回填
    if not order_main.get("userId"):
        order_main["userId"] = order_main.get("createBy") or get_order_defaults().get("userId", "")

    return order_main


def check_order_submit(session: requests.Session, info: dict) -> bool:
    """
    提交前校验订单信息。

    POST /api/business/order/submit/check
    请求: info（JSON 编码）
    返回: True 通过校验，False 校验不通过
    """
    url = f"{BASE_URL}{ENDPOINT_ORDER_SUBMIT_CHECK}"
    resp = session.post(url, json=info,
                        headers={**HEADERS, "Content-Type": "application/json"},
                        timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        logger.warning("校验不通过: %s", data.get("msg", data))
        return False
    return True


def submit_order(session: requests.Session, info: dict) -> dict:
    """
    提交报关资料。

    POST /api/business/order/submit
    请求: info（JSON 编码）
    返回: API 响应 dict
    """
    url = f"{BASE_URL}{ENDPOINT_ORDER_SUBMIT}"
    resp = session.post(url, json=info,
                        headers={**HEADERS, "Content-Type": "application/json"},
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


def submit_customs_for_order(session: requests.Session, order_no: str) -> bool:
    r"""
    单个订单的完整报关资料提交流程：
      1. 查 order_id
      2. get/info 获取订单详情
      3. submit/check 提交前校验
      4. submit 执行提交

    :param session:   已登录的 requests.Session
    :param order_no:  订单号（如 WT26SHUY0015）
    :return:          True 提交成功，False 失败
    """
    # Step 1: 查 order_id
    logger.info("查询订单 id: %s", order_no)
    order_id = query_order_id(session, order_no)
    if not order_id:
        logger.warning("未找到订单 %s", order_no)
        return False
    logger.info("id=%s", order_id)

    # Step 2: 获取订单详情
    info = get_order_info(session, order_id)
    if info is None:
        return False

    # Step 3: 提交前校验
    if not check_order_submit(session, info):
        return False

    # Step 4: 执行提交
    result = submit_order(session, info)
    logger.info("提交: %s", result.get("msg", result))
    return result.get("code") == 200 or "受理的订单不可修改" in result.get("msg")
