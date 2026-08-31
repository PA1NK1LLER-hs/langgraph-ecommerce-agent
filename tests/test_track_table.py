# -*- coding: utf-8 -*-
"""亚马逊轨迹跟踪表 RPA task 的配置层测试。

不触发任何真实业务（不下载领星库存、不开紫鸟浏览器）：
只验证配置解析、run() 的缺参/非法参数报错、工具注册。
"""

import os
import sys
from pathlib import Path

import pytest

# 复用 conftest 的 sys.path 引导（src/ 已在 conftest 注入）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from skills.rpa.common import config
from skills.rpa.tasks.track_table import flow
from skills.rpa.tasks.track_table.manifest import DESCRIPTION, TrackTableArgs


def _norm_store_path(label: str) -> Path:
    """根据店铺配置推导输出路径（与 flow.run 的逻辑一致）。"""
    cfg = config.TRACK_TABLE_STORES[label]
    return Path(cfg["outDir"]) / f"{label}轨迹跟踪表.xlsx"


# ── 1. 配置解析 ──

def test_track_table_stores_parsed_from_env():
    """.env 配置解析出两个店铺，outDir 为合法 UNC，inventory 过滤正确。"""
    stores = config.TRACK_TABLE_STORES
    assert isinstance(stores, dict)
    assert set(stores.keys()) == {"巧逗豆", "天安"}

    qdd = stores["巧逗豆"]
    assert qdd["browserName"] == "巧逗豆-US"
    assert qdd["browserOauth"] == "27153125947492"
    # UNC 前缀必须是双反斜杠
    assert qdd["outDir"].startswith("\\\\")
    assert qdd["outDir"].endswith("16.亚马逊轨迹跟踪表")
    assert qdd["inventory"] == {"美国": "巧逗豆-US", "加拿大": "巧逗豆-CA"}

    tianan = stores["天安"]
    assert tianan["browserName"] == "上海天安-US本土店"
    assert tianan["browserOauth"] == "27678664116395"
    assert tianan["outDir"].startswith("\\\\")
    assert tianan["outDir"].endswith("天安轨迹表+跟踪表")
    assert tianan["inventory"] == {"美国": "天安-Fond&Found-US-US", "加拿大": "天安-Fond&Found-US-CA"}


def test_related_config_set():
    """领星凭证与三条共享盘路径均已配置。"""
    assert config.LINGXING_USERNAME
    assert config.LINGXING_PASSWORD
    assert config.TRACK_TABLE_CONTRACT_PATH
    assert config.TRACK_TABLE_PASSKEY_TEMPLATE
    assert config.TRACK_TABLE_INVENTORY_SAVE_PATH


def test_output_path_derivation_matches_original():
    """输出文件名推导 = {outDir}/{标签}轨迹跟踪表.xlsx（与原版命名一致）。"""
    assert _norm_store_path("巧逗豆").name == "巧逗豆轨迹跟踪表.xlsx"
    assert _norm_store_path("天安").name == "天安轨迹跟踪表.xlsx"


def test_parse_track_table_stores_bad_json_raises():
    """TRACK_TABLE_STORES 非法 JSON → 明确报错。"""
    with pytest.raises(RuntimeError, match="不是合法 JSON"):
        config._parse_track_table_stores("{not-json")


# ── 2. manifest schema ──

def test_manifest_default():
    """store 默认空（=处理全部店铺），description 说明真实业务需审批。"""
    args = TrackTableArgs()
    assert args.store == ""
    assert "领星" in DESCRIPTION and "审批" in DESCRIPTION


# ── 3. 缺参/非法参数报错（不触发真实业务） ──

def test_run_missing_stores_returns_error(monkeypatch):
    """TRACK_TABLE_STORES 未配置 → run({}) 返回 error，不触碰任何浏览器。"""
    monkeypatch.setattr(config, "TRACK_TABLE_STORES", {})
    result = flow.run({})
    assert result["status"] == "error"
    assert "TRACK_TABLE_STORES" in result["message"]


def test_run_unknown_store_returns_error(monkeypatch):
    """store 参数指向未配置的店铺 → 返回 error。"""
    monkeypatch.setattr(config, "TRACK_TABLE_STORES", {"巧逗豆": config.TRACK_TABLE_STORES["巧逗豆"]})
    result = flow.run({"store": "不存在的店"})
    assert result["status"] == "error"
    assert "不存在的店" in result["message"]


# ── 4. 工具注册 ──

def test_get_rpa_tools_registered():
    """rpa_update_track_table 已注册进 get_rpa_tools()。"""
    from skills.rpa.adapters import get_rpa_tools

    names = [t.name for t in get_rpa_tools()]
    assert "rpa_update_track_table" in names
