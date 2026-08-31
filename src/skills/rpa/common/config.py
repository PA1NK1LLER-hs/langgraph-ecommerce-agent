# -*- coding: utf-8 -*-
"""RPA 统一配置 — 全部从环境变量（.env）读取，不在代码里硬编码凭证/路径/店铺。

单一来源：.env 由 `src/config.py` 顶层 `load_dotenv` 统一加载，
本模块只做 `os.getenv` + 默认值 + 惰性校验（凭证/必填路径到真正调用时才报错）。
"""

import json
import os
import platform

IS_WINDOWS = platform.system() == "Windows"

# ── 紫鸟浏览器 ──

ZINIAO_CLIENT_PATH = os.getenv(
    "ZINIAO_CLIENT_PATH",
    R"C:\Program Files\ziniao\ziniao.exe" if IS_WINDOWS else "ziniao",
)
ZINIAO_SOCKET_PORT = int(os.getenv("ZINIAO_SOCKET_PORT", "16851"))


def ziniao_user() -> dict:
    """惰性校验：仅在 RPA 工具实际调用时才要求凭证已设置。"""
    company = os.getenv("ZINIAO_COMPANY")
    username = os.getenv("ZINIAO_USERNAME")
    if not company or not username:
        raise RuntimeError("ZINIAO_COMPANY / ZINIAO_USERNAME 未设置，紫鸟 RPA 无法运行")
    return {
        "company": company,
        "username": username,
        "password": os.getenv("ZINIAO_PASSWORD", ""),
    }


def _parse_stores(raw: str) -> dict[str, str]:
    """解析 ZINIAO_STORES：格式 "name:id,name:id"。"""
    stores: dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if ":" in pair:
            name, sid = pair.split(":", 1)
            stores[name.strip()] = sid.strip()
    return stores


ZINIAO_STORES = _parse_stores(os.getenv("ZINIAO_STORES", ""))


# ── 广告花费查询（各站点产品日广告花费）──

AD_SPEND_OUTPUT_DIR = os.getenv("AD_SPEND_OUTPUT_DIR", "")
AD_SPEND_TEMPLATE_FILE = (
    os.path.join(AD_SPEND_OUTPUT_DIR, "各站点产品日广告花费数据表_USD.xlsx")
    if AD_SPEND_OUTPUT_DIR else ""
)
AD_SPEND_CHECKPOINT_FILE = (
    os.path.join(AD_SPEND_OUTPUT_DIR, "checkpoint_ads.json")
    if AD_SPEND_OUTPUT_DIR else ""
)


def _parse_ad_spend_stores(raw: str) -> dict:
    """解析 AD_SPEND_STORES：JSON {"normal": {"browserName","browserOauth"}, "us_local": {...}}。"""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AD_SPEND_STORES 不是合法 JSON") from exc


AD_SPEND_STORES = _parse_ad_spend_stores(os.getenv("AD_SPEND_STORES", ""))


# ── Amazon 评论/星级采集 ──

AMAZON_REVIEW_EXCEL_PATH = os.getenv("AMAZON_REVIEW_EXCEL_PATH", "")


# ── 亚马逊轨迹跟踪表（领星ERP + 紫鸟爬取 + 写回共享盘跟踪表）──

LINGXING_USERNAME = os.getenv("LINGXING_USERNAME", "")
LINGXING_PASSWORD = os.getenv("LINGXING_PASSWORD", "")
TRACK_TABLE_CONTRACT_PATH = os.getenv("TRACK_TABLE_CONTRACT_PATH", "")
TRACK_TABLE_PASSKEY_TEMPLATE = os.getenv("TRACK_TABLE_PASSKEY_TEMPLATE", "")
TRACK_TABLE_INVENTORY_SAVE_PATH = os.getenv("TRACK_TABLE_INVENTORY_SAVE_PATH", "")


def _parse_track_table_stores(raw: str) -> dict:
    """解析 TRACK_TABLE_STORES：JSON，键为中文店铺标签，值为店铺配置。

    示例:
      {
        "巧逗豆": {
          "browserName": "巧逗豆-US",
          "browserOauth": "27153125947492",
          "outDir": r"\\\\192.168.10.27\\共享文件夹\\16.亚马逊轨迹跟踪表",
          "inventory": {"美国": "巧逗豆-US", "加拿大": "巧逗豆-CA"}
        },
        "天安": {...}
      }
    输出文件名由 outDir + 标签推导：{outDir}/{标签}轨迹跟踪表.xlsx、{outDir}/{标签}_处理日志.txt。
    """
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("TRACK_TABLE_STORES 不是合法 JSON") from exc


TRACK_TABLE_STORES = _parse_track_table_stores(os.getenv("TRACK_TABLE_STORES", ""))
