# -*- coding: utf-8 -*-
"""世贸通抬头报关主流程 — run(payload) -> {status, data, message}。

完整流程：
  1. 定位报关 Excel：
     - 显式传 excel_path → 单文件处理（向后兼容）
     - 未传 → 自动发现：读网络共享跟踪表（SHIMAOTONG_TRACKING_BASE）→
       定位待办合同目录 → 取目录内第一个 xlsx → 批处理（原始版闭环）
  2. 从 .env 读取凭证并登录世贸通（验证码自动识别，批处理只登录一次复用 session）
  3. 逐个 Excel：检测子订单 → 生成下一个 orderNo（序号跨文件递增）→ 构建保存
     （订单号冲突时自动提取新号重试；单文件失败不中断整批）
  4. 可选：对保存成功的订单提交报关资料（submit_customs=True 时）
  5. 汇总结果

契约同 `skills/rpa/tasks/*/flow.py`：统一 run(payload) -> {status, data, message}。
"""

import glob
import logging
import os
import re
from typing import Optional

import requests

from .config import shimaotong_credentials
from .auth import login
from .order_builder import detect_orders, build_order
from .api_client import save_order, fetch_next_order_no, submit_customs_for_order, find_order_folders

logger = logging.getLogger(__name__)


def _extract_order_no_from_msg(msg: str) -> Optional[str]:
    """从错误消息中提取系统分配的新订单号，如 WT26SHUY0011。"""
    m = re.search(r'WT26SHUY\d+', str(msg))
    return m.group(0) if m else None


def _save_with_retry(session: requests.Session, po_order_no: str, order: dict, new_order_no: str,
                     max_retries: int = 10) -> tuple:
    """
    保存订单，遇到"订单号已使用"时自动提取系统分配的新号并重试。

    Args:
        session:        已登录 session
        po_order_no:    PO 子订单号（如 "PO260707003-3"），用作 contractNo
        order:          订单 payload
        new_order_no:   初始订单号
        max_retries:    最大重试次数

    Returns:
        (API 响应 dict, 最终使用的 orderNo)
    """
    current_order_no = new_order_no
    for attempt in range(max_retries):
        order["orderNo"] = current_order_no
        order["contractNo"] = po_order_no
        result = save_order(session, order)

        if result.get("code") == 200:
            return result, current_order_no

        msg = str(result.get("msg", ""))
        assigned = _extract_order_no_from_msg(msg)
        if assigned and "订单号已使用" in msg:
            logger.info("订单号 %s 已使用，改用系统分配号 %s 重试", current_order_no, assigned)
            current_order_no = assigned
            continue

        # 非冲突错误，直接返回
        return result, current_order_no

    return {"code": -1, "msg": f"重试{max_retries}次后仍失败"}, current_order_no


def _discover_excel_files() -> list[str]:
    """从网络共享跟踪表自动发现待办报关 Excel（原始版 main.py 闭环）。

    流程：`find_order_folders()` 读跟踪表"未做"sheet → 在"已进仓-{操作人}"
    目录搜索含合同编号的文件夹；每个文件夹取第一个 `*.xlsx`
    （排除 `~$` 临时锁文件，不遍历子文件夹）。

    Returns:
        待处理 Excel 路径列表；SHIMAOTONG_TRACKING_BASE 未配置或不可达时为空列表。
    """
    paths: list[str] = []
    folders = find_order_folders()
    for folder in folders:
        xlsx_files = glob.glob(os.path.join(folder, "*.xlsx"))
        xlsx_files = [f for f in xlsx_files if not os.path.basename(f).startswith("~$")]
        for x in xlsx_files:
            paths.append(x)
    return paths


def _process_one_file(session: requests.Session, excel_path: str, submit_customs: bool,
                      target_orders: list[str] | None, current_num: int | None) -> dict:
    """处理单个报关 Excel：检测子订单 → 生成订单号 → 构建保存 → 可选提交报关资料。

    Args:
        session:        已登录 session（批处理跨文件复用）
        excel_path:     报关 Excel 路径
        submit_customs: 是否提交报关资料
        target_orders:  指定子订单号列表（None/空 = 全部）
        current_num:    传入时沿用该订单号序号（批处理跨文件递增）；
                        None 则重新查询订单列表取最大号 + 1

    Returns:
        dict: {excel_path, results, summary, error, next_num}
        error 非空表示该文件处理失败（如无子订单/解析异常），不影响其他文件。
        next_num 为处理后应接续的订单号序号（供批处理下一文件使用）。
    """
    file_result = {
        "excel_path": excel_path,
        "results": {},
        "summary": "0/0 提交成功",
        "error": "",
        "next_num": current_num,
    }
    try:
        order_list = detect_orders(excel_path)
        if not order_list:
            file_result["error"] = f"{excel_path} 未找到任何子订单"
            return file_result

        if target_orders:
            # 只处理指定子订单；未知的 PO 号跳过
            order_list = [o for o in order_list if o in target_orders]
            if not order_list:
                file_result["error"] = f"Excel 中未找到目标子订单 {target_orders}"
                return file_result

        # 首次（或上一文件失败导致序号未续上）才查询订单列表
        if current_num is None:
            next_order_no = fetch_next_order_no(session)
            current_num = int(next_order_no[len("WT26SHUY"):])

        results: dict = {}
        order_no_map: dict = {}  # {po_order_no: wt_order_no}
        for po_order_no in order_list:
            new_order_no = f"WT26SHUY{current_num:04d}"
            try:
                order = build_order(session, excel_path, po_order_no, new_order_no)
                # 保存，订单号冲突时自动提取新号重试
                result, final_order_no = _save_with_retry(session, po_order_no, order, new_order_no)
                results[po_order_no] = {"code": result.get("code"), "msg": result.get("msg", result)}
                if result.get("code") == 200:
                    order_no_map[po_order_no] = final_order_no
                    assigned_num = int(final_order_no[len("WT26SHUY"):])
                    current_num = assigned_num + 1
            except Exception as exc:
                results[po_order_no] = {"code": -1, "msg": str(exc)}
                logger.exception("保存子订单 %s 异常", po_order_no)

        # 提交报关资料（仅已保存成功的，且显式开启）
        if submit_customs and order_no_map:
            for po_order_no, wt_order_no in order_no_map.items():
                ok = submit_customs_for_order(session, wt_order_no)
                results[po_order_no]["submitted"] = ok
        elif submit_customs and not order_no_map:
            logger.warning("submit_customs=True 但 %s 无保存成功的订单，跳过提交报关资料", excel_path)

        success = sum(1 for r in results.values() if r.get("code") == 200)
        file_result["results"] = results
        file_result["summary"] = f"{success}/{len(results)} 提交成功"
        file_result["next_num"] = current_num
        return file_result

    except Exception as exc:
        logger.exception("处理文件 %s 异常", excel_path)
        file_result["error"] = str(exc)
        return file_result


def run(payload: dict) -> dict:
    """
    世贸通抬头报关主流程。

    Args:
        payload:
            excel_path:      报关 Excel 文件路径（明细6 sheet）。留空则自动发现批处理。
            submit_customs:  是否提交报关资料（默认 False，只保存订单）
            target_orders:   指定要处理的子订单号列表（逗号分隔），None/空表示全部

    Returns:
        {status: "success"|"error", data: ..., message: ...}
        data = {files: [{excel_path, results, summary, error}], summary: "x/y 提交成功"}
        单文件模式额外保留 data.results / data.excel_path（向后兼容）。
    """
    excel_path = ((payload or {}).get("excel_path") or "").strip()
    submit_customs = bool((payload or {}).get("submit_customs", False))
    target_raw = (payload or {}).get("target_orders", "") or ""
    target_orders = [t.strip() for t in target_raw.split(",") if t.strip()] or None

    try:
        # 1. 定位待处理 Excel：显式路径 → 单文件；否则自动发现 → 批处理
        excel_paths: list[str] = []
        if excel_path:
            excel_paths = [excel_path]
        else:
            excel_paths = _discover_excel_files()
            if not excel_paths:
                return {"status": "error", "data": "",
                        "message": "缺少 excel_path，且自动发现未找到待办报关 Excel（检查 SHIMAOTONG_TRACKING_BASE 网络共享与跟踪表）"}

        # 2. 登录（凭证来自 .env，惰性校验；批处理只登录一次复用 session）
        creds = shimaotong_credentials()
        session, login_res = login(creds["username"], creds["password"])
        logger.info("登录: %s", login_res.get("msg"))

        # 3. 逐个 Excel 处理（单文件失败不中断整批；订单号序号跨文件递增）
        files = []
        current_num: int | None = None
        for path in excel_paths:
            logger.info("处理: %s", path)
            one = _process_one_file(session, path, submit_customs, target_orders, current_num)
            current_num = one.get("next_num", current_num)
            files.append(one)

        # 4. 汇总
        total_success = sum(
            sum(1 for r in f["results"].values() if r.get("code") == 200) for f in files
        )
        total_orders = sum(len(f["results"]) for f in files)
        data = {
            "files": [
                {"excel_path": f["excel_path"], "results": f["results"], "summary": f["summary"],
                 "error": f.get("error", "")}
                for f in files
            ],
            "summary": f"{total_success}/{total_orders} 提交成功",
        }
        if len(files) == 1:
            data["results"] = files[0]["results"]
            data["excel_path"] = files[0]["excel_path"]

        # 全部文件均失败（无订单/解析异常）→ 视为整体失败，返回首个错误信息
        if not any(not f.get("error") for f in files):
            first_err = next((f["error"] for f in files if f.get("error")), "未找到任何可处理的订单")
            return {"status": "error", "data": data, "message": f"世贸通抬头报关失败: {first_err}"}

        return {"status": "success", "data": data, "message": data["summary"]}

    except Exception as exc:
        logger.exception("世贸通抬头报关失败")
        return {"status": "error", "data": "", "message": f"世贸通抬头报关失败: {exc}"}
