# -*- coding: utf-8 -*-
"""断点续写 — 商品-日期级别的查询进度持久化。

从 `ExpenseVerification/广告查询/ziniao_playwright_http_py3_v2.py` 迁移。
断点键格式："{country}|{date_str}|{product}" -> spend 值（'' 表示已查但无花费）。
"""

import json
import os
import threading
from datetime import datetime

from .excel_io import write_result_to_excel

CHECKPOINT_LOCK = threading.Lock()


def load_checkpoint(filepath: str) -> dict:
    """加载断点文件，返回 {"country|date_str|product": spend_value, ...}。"""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"✓ 加载断点文件: {filepath} ({len(data)} 条记录)")
        return data
    except Exception as e:
        print(f"⚠ 加载断点文件失败: {e}，将从头开始")
        return {}


def save_checkpoint(filepath: str, checkpoint_dict: dict) -> None:
    """保存断点文件（线程安全，原子写入）。"""
    with CHECKPOINT_LOCK:
        try:
            dirpath = os.path.dirname(filepath)
            if dirpath and not os.path.exists(dirpath):
                os.makedirs(dirpath, exist_ok=True)
            tmp = filepath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(checkpoint_dict, f, ensure_ascii=False, indent=2)
            os.replace(tmp, filepath)
        except Exception as e:
            print(f"⚠ 保存断点文件失败: {e}")


def format_checkpoint_summary(checkpoint_dict: dict) -> str:
    """格式化断点摘要（按国家-日期分组统计商品数）。"""
    if not checkpoint_dict:
        return "断点文件为空"
    by_country_date: dict = {}
    for key in checkpoint_dict:
        parts = key.split("|", 2)
        if len(parts) == 3:
            c, d, _p = parts
            k2 = f"{c}|{d}"
            by_country_date[k2] = by_country_date.get(k2, 0) + 1
    lines = [f"断点共 {len(checkpoint_dict)} 条记录:"]
    for k2, cnt in sorted(by_country_date.items()):
        lines.append(f"  {k2}: {cnt}个商品")
    return "\n".join(lines)


def write_checkpoint_to_excel(checkpoint_dict: dict, original_result: dict, dir_path: str) -> None:
    """手动恢复：将断点文件中所有数据写入 Excel（不重新查询）。"""
    if not checkpoint_dict:
        print("断点文件为空，无需恢复")
        return

    date_slices: dict = {}
    for key, spend in checkpoint_dict.items():
        parts = key.split("|", 2)
        if len(parts) != 3:
            continue
        country, date_str, product = parts
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        date_slices.setdefault(d, {})
        for op, sites in original_result.items():
            if country in sites:
                date_slices[d].setdefault(op, {})
                date_slices[d][op].setdefault(country, {})
                date_slices[d][op][country][product] = spend
                break

    for d in sorted(date_slices.keys()):
        write_result_to_excel(date_slices[d], dir_path, d)


def get_checkpoint_completed_dates(checkpoint_data: dict, store_tasks: list, all_dates: list) -> set:
    """根据断点判断哪些日期已完全完成（所有店铺/国家/商品都有记录）。"""
    if not checkpoint_data:
        return set()
    completed = set()
    for d in all_dates:
        ds = d.strftime("%Y-%m-%d")
        all_done = True
        for task in store_tasks:
            for country, products in task.get("country_products", {}).items():
                for p in products:
                    if p and p != "/":
                        key = f"{country}|{ds}|{p.strip()}"
                        if key not in checkpoint_data:
                            all_done = False
                            break
                if not all_done:
                    break
            if not all_done:
                break
        if all_done and store_tasks:
            completed.add(d)
    return completed
