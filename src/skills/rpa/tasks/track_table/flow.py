# -*- coding: utf-8 -*-
"""亚马逊轨迹跟踪表更新流程。

从 `ExpenseVerification/track_table/update_excel.py`（巧逗豆）与
`update_excel_tianan.py`（上海天安）合并迁移：两脚本仅店铺常量不同
（browserName/browserOauth/outDir/库存店铺过滤），已全部外置到
`config.TRACK_TABLE_STORES`，店铺配置键用中文标签（巧逗豆/天安），
输出文件名 = `{outDir}/{标签}轨迹跟踪表.xlsx`、`{outDir}/{标签}_处理日志.txt`。

入口 `run(payload) -> {status, data, message}`，流程与原版 main() 一致：
领星ERP下载FBA库存 → 紫鸟生命周期（kill/start/update_core）→ 多店铺并发爬取 → get_exit。

与原版的差异：
  - 删除 `await page.pause()` 调试断点（会挂起）
  - 删除 `from track_table.excute_excel import get_local_only_data` 死依赖
  - 硬编码凭证/路径/店铺 → config + payload
  - `exit()` → 抛 RuntimeError，由 run() 归一化为 {status, data, message}
  - 死代码（_save_single_sheet / _process_sell_batch / _write_back_df）不再保留
  - 单店铺失败不再中断其它店铺（线程池按 future 收集结果）
"""

import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd
import requests
from playwright.async_api import async_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from skills.rpa.common import config
from skills.rpa.common.ziniao_client import ZINIAO_LOCK

MAX_CONCURRENT_TABS = 3  # 同一浏览器内同时查询的货件tab数
SKIP_STATUS_PATTERN = '拦截|退运|取消'  # 跳过查询但保留写入的状态

logger = logging.getLogger(__name__)
_write_lock = threading.Lock()


# ==================== 紫鸟客户端生命周期（逐字移植自 update_excel.py 原版） ====================
# 原版将这些函数定义在本脚本内（含 async 的浏览器会话函数）。这里同样保持自包含，
# 不复用 ziniao_client 的同名同步版（其 open_ip_check/close_store 等是 sync，
# 与本任务 async 流程不匹配，曾导致 "'coroutine' object has no attribute 'contexts'"）。
# 与原版的唯一差异：exit() → raise RuntimeError（原版 exit() 会直接杀掉 agent/MCP 进程），
# 硬编码的 client_path / socket_port / user_info → config。

is_windows = platform.system() == 'Windows'
is_mac = platform.system() == 'Darwin'
is_linux = platform.system() == 'Linux'

# 需要从系统角标将紫鸟浏览器完全退出后再运行
client_path = config.ZINIAO_CLIENT_PATH
socket_port = config.ZINIAO_SOCKET_PORT
user_info = config.ziniao_user()


def kill_process(version: Literal["v5", "v6"]):
    """
    杀紫鸟客户端进程
    :param version: 客户端版本
    """
    if is_windows:
        if version == "v5":
            process_name = 'SuperBrowser.exe'
        else:
            process_name = 'ziniao.exe'
        os.system('taskkill /f /t /im ' + process_name)
        time.sleep(3)


def start_browser():
    """
    启动客户端
    :return:
    """
    try:
        if is_windows:
            cmd = [client_path, '--run_type=web_driver', '--ipc_type=http', '--port=' + str(socket_port)]
        elif is_mac:
            cmd = ['open', '-a', client_path, '--args', '--run_type=web_driver', '--ipc_type=http',
                   '--port=' + str(socket_port)]
        elif is_linux:
            cmd = [client_path, '--no-sandbox', '--run_type=web_driver', '--ipc_type=http',
                   '--port=' + str(socket_port)]
        else:
            raise RuntimeError("未知平台，无法启动紫鸟客户端")
        subprocess.Popen(cmd)
        time.sleep(5)
    except Exception:
        raise RuntimeError('start browser process failed: ' + traceback.format_exc())


def update_core():
    """
    下载所有内核，打开店铺前调用，需客户端版本5.285.7以上
    因为http有超时时间，所以这个action适合循环调用，直到返回成功
    """
    data = {
        "action": "updateCore",
        "requestId": str(uuid.uuid4()),
    }
    data.update(user_info)
    while True:
        result = send_http(data)
        logger.info("updateCore 响应: %s", result)
        if result is None:
            logger.info("等待客户端启动...")
            time.sleep(2)
            continue
        if result.get("statusCode") is None or result.get("statusCode") == -10003:
            logger.info("当前版本不支持此接口，请升级客户端")
            return
        elif result.get("statusCode") == 0:
            logger.info("更新内核完成")
            return
        else:
            logger.info("等待更新内核: %s", json.dumps(result))
            time.sleep(2)


def send_http(data):
    """
    通讯方式
    :param data:
    :return:
    """
    try:
        url = 'http://127.0.0.1:{}'.format(socket_port)
        response = requests.post(url, json.dumps(data).encode('utf-8'), timeout=120)
        return json.loads(response.text)
    except Exception as err:
        logger.warning("send_http 失败: %s", err)


def open_store(store_info, isWebDriverReadOnlyMode=1, isprivacy=0, isHeadless=0, cookieTypeSave=0, jsInfo=""):
    request_id = str(uuid.uuid4())
    data = {
        "action": "startBrowser"
        , "isWaitPluginUpdate": 0
        , "isHeadless": isHeadless
        , "requestId": request_id
        , "isWebDriverReadOnlyMode": isWebDriverReadOnlyMode
        , "cookieTypeLoad": 0
        , "cookieTypeSave": cookieTypeSave
        , "runMode": "1"
        , "isLoadUserPlugin": False
        , "pluginIdType": 1
        , "privacyMode": isprivacy
    }
    data.update(user_info)

    if store_info.isdigit():
        data["browserId"] = store_info
    else:
        data["browserOauth"] = store_info

    if len(str(jsInfo)) > 2:
        data["injectJsInfo"] = json.dumps(jsInfo)

    r = send_http(data)
    if str(r.get("statusCode")) == "0":
        return r
    elif str(r.get("statusCode")) == "-10003":
        raise RuntimeError(f"login Err {json.dumps(r, ensure_ascii=False)}")
    else:
        raise RuntimeError(f"Fail {json.dumps(r, ensure_ascii=False)}")


async def close_store(browser_oauth):
    request_id = str(uuid.uuid4())
    data = {
        "action": "stopBrowser"
        , "requestId": request_id
        , "duplicate": 0
        , "browserOauth": browser_oauth
    }
    data.update(user_info)

    r = send_http(data)
    if str(r.get("statusCode")) == "0":
        return r
    elif str(r.get("statusCode")) == "-10003":
        raise RuntimeError(f"login Err {json.dumps(r, ensure_ascii=False)}")
    else:
        raise RuntimeError(f"Fail {json.dumps(r, ensure_ascii=False)}")


async def get_browser_context(playwright, port):
    """通过 CDP 连接已打开的店铺浏览器，返回其第一个 context。"""
    browser = await playwright.chromium.connect_over_cdp("http://127.0.0.1:" + str(port))
    context = browser.contexts[0]
    return context


async def open_ip_check(browser_context, ip_check_url):
    """
    打开ip检测页检测ip是否正常
    :param browser_context: playwright浏览器会话
    :param ip_check_url ip检测页地址
    :return 检测结果
    """
    try:
        page = browser_context.pages[0]

        await page.goto(ip_check_url)
        success_button = page.locator('//button[contains(@class, "styles_btn--success")]')
        await success_button.wait_for(timeout=60000)  # 等待查找元素60秒
        logger.info("ip检测成功")
        return True
    except PlaywrightTimeoutError:
        logger.warning("ip检测超时")
        return False
    except Exception as e:
        logger.warning("ip检测异常: %s", traceback.format_exc())
        return False


async def open_launcher_page(browser_context, launcher_page):
    page = browser_context.pages[0]
    await page.goto(launcher_page)

    await page.wait_for_load_state(state="load", timeout=60000)
    await check_verified_status(page)
    return page


def get_exit():
    """
    关闭客户端
    :return:
    """
    data = {"action": "exit", "requestId": str(uuid.uuid4())}

    data.update(user_info)

    logger.info("@@ get_exit...")
    send_http(data)

# 领星下载的 FBA 库存文件路径（run() 下载成功后设置）
_INVENTORY_PATH = None

# 领星下载最近一次失败的真实原因（供 run() 拼进返回消息，便于排查）
_LINGXING_LAST_ERROR = None

# 指定Excel列的输出顺序（按业务流程排列）
COLUMN_ORDER = [
    '站点',
    '出运合同号',
    '进仓单号',
    '货件编码',
    '品名',
    '店铺msku',
    '实际箱数',
    '创建时间',
    '仓库',
    '出口日期',
    '到港时间',
    '实际接收',
    '接收数量差',
    '状态',
    '库存数量',
    '更新时间',
    '实际接收-校正后',
    '货本',
    '售卖批次',
]


# ==================== 断点续跑（日志按店铺隔离） ====================

def _load_completed_sheets(log_path):
    """从日志文件读取已完成的sheet列表（用于断点续跑）"""
    if not os.path.exists(log_path):
        return set()
    with open(log_path, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())


def _mark_sheet_done(log_path, sheet_name):
    """追加一个已完成sheet到日志文件"""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"{sheet_name}\n")


# ==================== 同步入口：单店铺流程 ====================

def process_one_browser(browser):
    """同步主函数：打开店铺 → 读取数据 → 断点续跑 → 按Sheet爬取并增量保存。

    browser dict（由 run() 从 config.TRACK_TABLE_STORES 构建）:
        label, browserName, browserOauth, outDir, outPath, logPath, inventory
    """
    store_id = browser.get('browserOauth')
    store_name = browser.get("browserName")
    label = browser.get("label", store_name)
    out_path = browser["outPath"]
    log_path = browser["logPath"]
    store_inventory = browser.get("inventory", {})

    # ---- 并行：后台预读合同数据（本店铺跟踪表为源），同时打开店铺 ----
    print(f"=====打开店铺：{store_name}=====")
    with ThreadPoolExecutor(max_workers=1) as _executor:
        contract_future = _executor.submit(
            fill_arrival_time_from_contract, out_path, config.TRACK_TABLE_CONTRACT_PATH
        )
        ret_json = open_store(store_id)          # 主线程：启动浏览器
        print(ret_json)
        output_by_sheet = contract_future.result()  # 等待合同数据就绪

    # ---- 同步：断点续跑 ----
    completed_sheets = _load_completed_sheets(log_path)
    if completed_sheets:
        print(f"  已完成Sheet: {completed_sheets}")
        for sheet_name in completed_sheets:
            if sheet_name in output_by_sheet:
                del output_by_sheet[sheet_name]
                print(f"  ⏭ 跳过: {sheet_name}")

    if not output_by_sheet:
        print("所有Sheet均已完成，无需处理")
        return {"label": label, "status": "success", "message": "所有Sheet均已完成，无需处理"}

    first_save = not os.path.exists(out_path)

    # ---- 异步：按Sheet爬取 ----
    asyncio.run(_scrape_all(ret_json, output_by_sheet, store_name, first_save,
                            out_path, log_path, store_inventory))
    return {"label": label, "status": "success"}


# ==================== 异步：切换语言 ====================

async def _switch_language_to_chinese(page):
    try:
        langguage = page.locator('//*[@id="navbar"]/div[3]/div/div[5]')
        if await langguage.is_visible():
            await langguage.hover(timeout=5000)

            await langguage.click(force=True)

            locale_item = page.locator("//a[@data-test-tag='locale-list-item-zh_CN']")
            if await locale_item.is_visible() and await locale_item.is_enabled():
                await locale_item.hover(force=True, timeout=5000)
                await locale_item.click(force=True, timeout=5000)

        langguage = page.locator("//div[@id='ngstrim-settings-dropdown']")
        if await langguage.is_visible():
            await langguage.click(force=True, timeout=6000)

            section = langguage.locator("//div[@class='ngstrim-dropdown-section']/div[3]")
            await section.hover(timeout=5000)
            await section.click(force=True)

            await page.get_by_role("menuitem", name="中文(简体)").click(force=True, timeout=6000)
    except Exception as e:
        print(f"切换语言失败: {e}")
        pass


# ==================== 异步：爬取调度 ====================

async def _scrape_all(ret_json, output_by_sheet, store_name, first_save,
                      out_path, log_path, store_inventory):
    """异步：连接浏览器 → IP检测 → 切换语言 → 按Sheet逐个处理 → 每个Sheet跑完立刻保存"""
    async with async_playwright() as playwright:
        browser_context = await get_browser_context(playwright, ret_json.get('debuggingPort'))
        if browser_context is None:
            print(f"=====关闭店铺：{store_name}=====")
            await close_store(ret_json.get("browserOauth") or ret_json.get("browserId"))
            return

        # IP 检测
        ip_check_url = ret_json.get("ipDetectionPage")
        if not ip_check_url:
            print("ip检测页地址为空，请升级紫鸟浏览器到最新版")
            print(f"=====关闭店铺：{store_name}=====")
            await close_store(ret_json.get("browserOauth") or ret_json.get("browserId"))
            return

        ip_usable = await open_ip_check(browser_context, ip_check_url)
        if not ip_usable:
            print("ip检测不通过，请检查")
            print(f"=====关闭店铺：{store_name}=====")
            await close_store(ret_json.get("browserOauth") or ret_json.get("browserId"))
            return

        print("ip检测通过，打开店铺平台主页")

        # 打开主页 + 切换语言
        page = await open_launcher_page(browser_context, ret_json.get("launcherPage"))
        await _switch_language_to_chinese(page)

        # ---- 按Sheet处理：Sheet内按国家串行切换，货件并发查询 ----
        MAX_RETRY_ROUNDS = 2   # 每个国家最多重跑轮次
        sem = asyncio.Semaphore(MAX_CONCURRENT_TABS)
        print_lock = asyncio.Lock()
        fail_list = []          # 未完全匹配的货件编号
        inventory_lookup = _build_inventory_lookup(store_inventory)  # 库存文件只读一次

        # 快照爬取前的原始状态：重跑分流必须用原始状态判断，
        # 否则首次抓取把状态写成"已完成"后，重跑会误走"已完成"捷径、不再进入抓取逻辑
        original_status = {}
        for _country_dict in output_by_sheet.values():
            for _shipment_dict in _country_dict.values():
                for _sn, _df in _shipment_dict.items():
                    original_status[_sn] = str(_df['状态'].iloc[0]) if len(_df) > 0 else ''

        total_sheets = len(output_by_sheet)
        completed_count = 0

        for sheet_name, country_dict in output_by_sheet.items():
            print(f"\n{'='*60}")
            print(f"  📋 Sheet: {sheet_name}")
            print(f"{'='*60}")

            sheet_failed = set()  # 该Sheet重跑后仍失败的货件（有失败则不写入完成日志）

            for country, shipment_dict in country_dict.items():
                # 检查是否已在目标国家，不在则切换
                now_country = page.locator(
                    f"//div[@class='dropdown-account-switcher-header'][contains(.,'{country}')]"
                )
                if await now_country.count() == 0:
                    await _switch_country(page, country)

                # 收集已完结货件
                skipped_shipments = []
                for sn, df in shipment_dict.items():
                    all_skip = df['状态'].astype(str).str.contains(
                        SKIP_STATUS_PATTERN, na=False
                    ).all()
                    if all_skip:
                        skipped_shipments.append(sn)

                if skipped_shipments:
                    print(f"  [{country}] 跳过 {len(skipped_shipments)} 个已完结: {', '.join(skipped_shipments)}")

                async def _query_with_limit(shipment_no, df):
                    async with sem:
                        return await _query_one_shipment(browser_context, shipment_no, df,
                                                         print_lock, fail_list, original_status)

                # 首轮：并发查询该国家下所有货件
                active_shipments = {sn: df for sn, df in shipment_dict.items()
                                    if sn not in skipped_shipments}
                tasks = [_query_with_limit(sn, df) for sn, df in active_shipments.items()]
                if tasks:
                    await asyncio.gather(*tasks)

                # 重跑失败的货件
                for retry_round in range(MAX_RETRY_ROUNDS):
                    if not fail_list:
                        break
                    retry_sns = list(set(fail_list))  # 去重
                    fail_list.clear()
                    print(f"  🔄 [{country}] 第{retry_round+1}次重跑 {len(retry_sns)} 个: {retry_sns}")

                    retry_tasks = [_query_with_limit(sn, active_shipments[sn])
                                   for sn in retry_sns if sn in active_shipments]
                    if retry_tasks:
                        await asyncio.gather(*retry_tasks)

                # 重跑后仍失败的货件计入该Sheet失败集合，并清空避免泄漏到下一个国家
                if fail_list:
                    sheet_failed.update(fail_list)
                    fail_list.clear()

                print(f"  [{country}] 处理完成")

            # 该Sheet所有国家都跑完 → 填充库存 → 分配售卖批次 → 一次保存
            _fill_inventory_from_file(output_by_sheet, sheet_name, lookup=inventory_lookup)
            # 构建合并DataFrame，在内存中分配售卖批次，最后一次性写入（避免中间读写的I/O浪费）
            all_rows = []
            for shipment_dict in output_by_sheet[sheet_name].values():
                all_rows.extend(shipment_dict.values())
            combined_df = pd.concat(all_rows).drop(columns=['来源Sheet'], errors='ignore')
            _apply_sell_batch_to_df(combined_df)

            if sheet_failed:
                # 有货件失败：保存数据但不写入完成日志，下次运行重新处理该Sheet
                print(f"  ⚠ Sheet [{sheet_name}] 有 {len(sheet_failed)} 个货件失败，"
                      f"不写入完成日志，下次运行将重新处理: {sorted(sheet_failed)}")
                _save_df_to_sheet(combined_df, sheet_name, first_save, mark_done=False,
                                  out_path=out_path, log_path=log_path)
                print(f"  ⚠ 未计入完成进度（已完成 {completed_count}/{total_sheets} 个）")
            else:
                _save_df_to_sheet(combined_df, sheet_name, first_save,
                                  out_path=out_path, log_path=log_path)
                completed_count += 1
                print(f"  ✅ 已完成第 {completed_count}/{total_sheets} 个Sheet: {sheet_name}")
            first_save = False

        print(f"=====关闭店铺：{store_name}=====")
        await close_store(ret_json.get("browserOauth") or ret_json.get("browserId"))


# ==================== 异步：切换国家 ====================

async def _switch_country(page, country):
    """切换到目标国家的 Amazon marketplace"""

    await page.goto(
        "https://sellercentral.amazon.com/account-switcher/default/merchantMarketplace",
        wait_until="load", timeout=60000
    )
    await check_verified_status(page)

    location = page.locator(f'//span[contains(text(), "{country}")]')
    await location.wait_for(timeout=60000)
    await location.click(force=True, timeout=5000)
    await asyncio.sleep(0.5)
    await page.locator("//kat-button[@label='选择账户']").click(force=True, timeout=5000)
    await page.wait_for_load_state(state="load", timeout=60000)


async def _query_one_shipment(browser_context, shipment_no, df, print_lock, fail_list=None,
                              original_status=None):
    """单个货件查询：按MSKU分类 → 按货件状态分流处理 → 关闭tab
    使用 print_lock 确保多个协程并发时打印不互相穿插
    original_status: 爬取前快照的原始状态映射。重跑分流必须用它判断，
    避免首次抓取写入的"已完成"导致重跑不再进入抓取逻辑"""
    print(f"  [{shipment_no}] 开始查询")
    page = None
    try:
        mskus = set(df['店铺msku'].astype(str).str.strip())
        total_msku_count = len(mskus)

        # ===== Phase 1: 按箱数分类 MSKU =====
        active = []    # [(msku, mask)]
        skip_count = 0

        for msku in mskus:
            mask = df['店铺msku'].astype(str).str.strip() == msku
            box_count = pd.to_numeric(df.loc[mask, '实际箱数'], errors='coerce')

            if (box_count == 0).all() or msku == "nan":
                print(f"  [{shipment_no}] {msku} 实际箱数为0，跳过")
                skip_count += 1
            else:
                active.append((msku, mask))

        if not active:
            print(f"  [{shipment_no}] 无有效MSKU需要处理")
            return

        # ===== Phase 2: 按货件状态分流 =====
        # 必须用爬取前的原始状态判断（同一货件所有行状态统一）：
        # 若读实时 df，首次抓取写入的"已完成"会让重跑误走捷径、跳过抓取
        if original_status is not None:
            first_status = original_status.get(shipment_no, '')
        else:
            first_status = str(df['状态'].iloc[0]) if len(df) > 0 else ''
        # 亚马逊状态分"已完成"与"已完成配送"：
        # 必须含"完成"且不含"配送"才算已完成，"已完成配送"仍需进入抓取逻辑
        shipment_done = ("完成" in first_status) and ("配送" not in first_status)

        if shipment_done:
            # 已完成：不爬取差异表，直接用现有数据计算校正后
            for msku, mask in active:
                numeric_amount = pd.to_numeric(df.loc[mask, '实际接收'], errors='coerce').fillna(0)
                box_count = pd.to_numeric(df.loc[mask, '实际箱数'], errors='coerce')
                df.loc[mask, '实际接收-校正后'] = np.where(numeric_amount > box_count, box_count, numeric_amount)
            print(f"  📦 [{shipment_no}] 已完成  |  直接计算校正后  |  MSKU: {len(active)}/{total_msku_count}")

        else:
            page = await browser_context.new_page()
            # 未完成：打开货件页面爬取差异表
            await page.goto(
                f"https://sellercentral.amazon.com/fba/inbound-shipment/summary/{shipment_no}/contents",
                timeout=60000
            )

            badge = page.locator("//kat-badge").first
            await badge.wait_for(state="visible", timeout=60000)
            status = await badge.get_attribute("label")

            # 写回状态和更新时间到该货件下所有行
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            iframe_loc = page.frame_locator("#iframe-shipment-content")
            # 定位复选框元素（iframe内）
            inner_check = iframe_loc.locator("kat-checkbox[id='discrepancies-checkbox'] >> div[role='checkbox']")
            aria_state = await inner_check.get_attribute("aria-checked")

            while aria_state == "true":
                await inner_check.click(force=True, timeout=5000)
                await page.wait_for_timeout(500)
                aria_state = await inner_check.get_attribute("aria-checked")

            # 提取差异表明细，匹配MSKU并写回实际接收

            table_rows = iframe_loc.locator(
                "//kat-table[@id='discrepancy-table']/kat-table-body/kat-table-row"
            )
            await table_rows.first.wait_for(state="visible", timeout=60000)

            # 先收集结果、不写回 df：中途异常时 df 保持原样，重跑仍会进入抓取逻辑
            msku_results = []   # [(msku, mask, amount, matched)]
            for msku, mask in active:
                row = table_rows.filter(has_text=f"{msku}")
                try:
                    await row.first.wait_for(state="visible", timeout=5000)
                except Exception as e:
                    print(f"{msku}:{e}")
                if await row.count() == 0:
                    msku_results.append((msku, mask, 0, False))
                    continue

                amount = (await row.first.locator("kat-table-cell").nth(3).locator("kat-link").get_attribute("label")).strip()
                msku_results.append((msku, mask, amount, True))

            # 全部提取成功后才统一写回 df
            for msku, mask, amount, matched in msku_results:
                df.loc[mask, '状态'] = status
                df.loc[mask, '更新时间'] = now_str
                if not matched:
                    continue
                try:
                    numeric_amount = float(amount.replace(',', ''))
                except ValueError:
                    numeric_amount = amount
                df.loc[mask, '实际接收'] = numeric_amount
                # 实际接收-校正后：接收数超过箱数时取箱数，否则取接收数
                box_count = pd.to_numeric(df.loc[mask, '实际箱数'], errors='coerce')
                df.loc[mask, '实际接收-校正后'] = np.where(numeric_amount > box_count, box_count, numeric_amount)
                # 计算接收数量差（实际接收 - 实际箱数）
                df.loc[mask, '接收数量差'] = numeric_amount - box_count
                # 负差值标记为"需申诉"（仅真正已完成：含"完成"且不含"配送"）
                need_appeal = df.loc[mask, '接收数量差'] < 0
                if need_appeal.any() and ("完成" in status) and ("配送" not in status):
                    df.loc[mask & need_appeal, '状态'] = "需申诉"

            matched_count = sum(1 for _, _, _, m in msku_results if m)
            async with print_lock:
                for msku_text, _, amount, matched in msku_results:
                    flag = "✓" if matched else "⚠"
                    print(f"      {flag} {msku_text}  →  {amount}")
                if matched_count + skip_count < total_msku_count:
                    unmatched = [a for a, _, _, m in msku_results if not m]
                    print(f"      ⚠ {shipment_no}表中未匹配MSKU: {unmatched}")
                    if fail_list is not None:
                        fail_list.append(shipment_no)
                else:
                    print(f"  📦 [{shipment_no}]  |  状态: {status}  |  匹配: {matched_count}/{total_msku_count}")

    except Exception as e:
        print(f"  ❌ 货件 {shipment_no} 抓取失败：{e}")
        if fail_list is not None:
            fail_list.append(shipment_no)
    finally:
        if page:
            try:
                await check_verified_status(page)
            except Exception as e:
                # 验证步骤失败不应崩掉整个货件流程，记录后照常关页
                print(f"  [货件 {shipment_no}] 验证步骤失败（不影响已抓取结果）: {e}")
            await page.wait_for_timeout(500)
            await page.close()


# ==================== 同步：售卖批次分配 ====================

def _parse_date(val):
    """解析到港时间：datetime/Timestamp直接用；字符串先直接解析，失败则清洗后重试"""
    if pd.isna(val):
        return pd.NaT
    if isinstance(val, (pd.Timestamp, datetime)):
        return val
    s = str(val).strip()
    # 先直接解析
    d = pd.to_datetime(s, errors='coerce')
    if not pd.isna(d):
        return d
    # 清洗后重试：取第一行、去中文、去末尾非日期符号
    s = s.split('\n')[0].strip()
    s = re.sub(r'[一-鿿]+', '', s).strip()
    s = re.sub(r'[^0-9/\-:]+\s*$', '', s).strip()
    return pd.to_datetime(s, errors='coerce')


def _apply_sell_batch_to_df(df):
    """在内存DataFrame上分配售卖批次（FIFO逆向），返回修改后的DataFrame

    1. 过滤掉 拦截|退运|取消|申诉 状态，以及实际接收-校正后为0 的行（仅用于计算，不影响保存）
    2. 按 (站点, 店铺msku) 分组
    3. 清洗到港时间，按时间排序（新→旧）
    4. FIFO逆向：库存从最新批次往前减，找到当前售卖批次标记为 current
    """
    if df.empty:
        return df

    # 每次重算前先清空所有售卖批次标记，避免上一轮残留的 current 未被覆盖
    df['售卖批次'] = ''

    # 过滤有效行用于计算：排除 拦截|退运|取消|申诉 状态，以及实际接收-校正后为0的数据
    recv_zero = pd.to_numeric(df['实际接收-校正后'], errors='coerce').eq(0)
    work = df[~(recv_zero)].copy()

    if work.empty:
        return df

    # 解析到港时间
    work['_arrival'] = work['到港时间'].apply(_parse_date)
    work = work.dropna(subset=['_arrival'])

    if work.empty:
        return df

    # 按 (站点, 店铺msku) 分组处理
    for (_site, _msku), grp in work.groupby(['站点', '店铺msku']):
        inv = pd.to_numeric(grp['库存数量'].iloc[0], errors='coerce')

        if pd.isna(inv) or inv <= 0:
            # 库存为0或无效 → 标记最晚批次为 current
            grp_desc = grp.sort_values('_arrival', ascending=False)
            df.loc[grp_desc.index[0], '售卖批次'] = 'current'
            continue

        # 从新到旧排序，找 current 批次
        grp = grp.sort_values('_arrival', ascending=False)
        remaining = inv

        for idx, row in grp.iterrows():
            recv = pd.to_numeric(row['实际接收-校正后'], errors='coerce')
            recv = recv if not pd.isna(recv) else 0

            if remaining <= 0:
                pass  # 库存已耗尽，更老的批次不标记
            elif remaining > recv:
                remaining -= recv  # 整批在库，继续往前找
            else:
                # remaining <= recv：库存从这一批开始消耗
                df.loc[idx, '售卖批次'] = 'current'
                remaining = 0

    return df


# ==================== 同步：FBA库存文件查询（店铺过滤由配置传入） ====================

def _build_inventory_lookup(store_inventory):
    """读取FBA仓库明细Excel一次，构建 (站点, MSKU) -> 库存数量 的查找表。

    store_inventory: {站点: 店铺过滤串}（如 {'美国':'巧逗豆-US','加拿大':'巧逗豆-CA'}），
    从 config.TRACK_TABLE_STORES[店铺].inventory 读取。
    库存数量 = FBA可售 + FBA待调仓。
    """
    global _INVENTORY_PATH
    if not _INVENTORY_PATH or not os.path.exists(_INVENTORY_PATH):
        print(f"  ⚠ FBA库存文件不存在: {_INVENTORY_PATH}")
        return {}

    inv_df = pd.read_excel(_INVENTORY_PATH, engine='openpyxl')

    # 计算库存 = FBA可售 + FBA待调仓
    inv_df['_库存'] = (
        pd.to_numeric(inv_df['FBA可售'], errors='coerce').fillna(0) +
        pd.to_numeric(inv_df['FBA待调仓'], errors='coerce').fillna(0)
    )

    # 按店铺筛选，构建 {(site_key, msku): 库存数量}
    lookup = {}
    for site_key, store_pattern in (store_inventory or {}).items():
        store_data = inv_df[inv_df['店铺'].astype(str).str.contains(store_pattern, na=False)]
        for _, row in store_data.iterrows():
            msku = str(row['MSKU']).strip()
            if msku and msku != 'nan':
                lookup[(site_key, msku)] = row['_库存']

    print(f"  📊 库存查找表构建完成: {len(lookup)} 条记录")
    return lookup


def _fill_inventory_from_file(output_by_sheet, sheet_name, lookup=None):
    """从FBA仓库明细Excel读取库存（按站点+MSKU匹配），填充到输出数据中

    若传入lookup则直接使用缓存，否则读取文件构建（向后兼容）。

    匹配规则：
      - 站点含"加拿大" → 加拿大的店铺过滤串
      - 否则 → 美国的店铺过滤串
      - 库存数量 = FBA可售 + FBA待调仓
    """
    if lookup is None:
        lookup = _build_inventory_lookup({})
        if not lookup:
            return

    # 遍历sheet下所有DataFrame，按站点+MSKU匹配填充库存
    country_dict = output_by_sheet.get(sheet_name, {})
    filled = 0
    unfilled = 0
    for country, shipment_dict in country_dict.items():
        site_key = '加拿大' if '加拿大' in str(country) else '美国'
        for shipment_no, df in shipment_dict.items():
            if '库存数量' not in df.columns:
                df['库存数量'] = ''
            for idx, row in df.iterrows():
                msku = str(row['店铺msku']).strip()
                key = (site_key, msku)
                if key in lookup:
                    df.at[idx, '库存数量'] = lookup[key]
                    filled += 1
                else:
                    df.at[idx, '库存数量'] = 0  # 未匹配到，清零避免残留旧数据
                    unfilled += 1

    print(f"  📊 [{sheet_name}] 库存填充: {filled} 条, 未匹配置零: {unfilled} 条")


# ==================== 同步：结果保存 ====================

def _save_df_to_sheet(df, sheet_name, first_save=False, mark_done=True, out_path=None, log_path=None):
    """将已处理好的DataFrame直接保存到Excel的指定Sheet（含列排序 + mark_done）

    mark_done=False 时只保存不写入完成日志（该Sheet存在失败货件，下次重跑）
    """
    if df.empty:
        return
    if not out_path or not log_path:
        raise RuntimeError("_save_df_to_sheet 缺少 out_path/log_path")

    with _write_lock:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        # 按指定列顺序排列
        ordered_cols = [c for c in COLUMN_ORDER if c in df.columns]
        remaining_cols = [c for c in df.columns if c not in ordered_cols]
        write_df = df[ordered_cols + remaining_cols]

        mode = 'w' if first_save else 'a'
        engine_kwargs = {} if first_save else {'if_sheet_exists': 'replace'}
        with pd.ExcelWriter(out_path, mode=mode, engine='openpyxl', **engine_kwargs) as writer:
            write_df.to_excel(writer, sheet_name=sheet_name, index=False)
        print(f"  ✅ 写入Sheet [{sheet_name}]: {len(write_df)} 行")
        if mark_done:
            _mark_sheet_done(log_path, sheet_name)


# ==================== 同步：passkey 验证码模板匹配 ====================

def _match_template(screenshot_bytes, template):
    """在页面截图中匹配模板，返回 (是否匹配, 点击x, 点击y)。纯CPU计算，供 asyncio.to_thread 调用。"""
    import cv2  # 惰性导入：opencv-python 仅在运行时需要（非基础依赖，避免破坏工具注册）

    screen_img = cv2.imdecode(np.frombuffer(screenshot_bytes, np.uint8), cv2.IMREAD_COLOR)
    res = cv2.matchTemplate(screen_img, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val < 0.8:  # 未匹配到按钮，说明已消失
        return False, 0, 0
    click_x = int(max_loc[0] + template.shape[1] // 2)
    click_y = int(max_loc[1] + template.shape[0] // 2)
    return True, click_x, click_y


async def check_verified_status(page):
    # 模板用绝对 UNC 路径（config.TRACK_TABLE_PASSKEY_TEMPLATE）。
    # 注意：cv2.imread 在 Windows 上遇中文/UNC 路径会读失败，
    # 所以改用 open() 读字节 + imdecode 解码
    def _load_template():
        import cv2  # 惰性导入：opencv-python 仅在运行时需要（非基础依赖）
        with open(config.TRACK_TABLE_PASSKEY_TEMPLATE, "rb") as f:
            return cv2.imdecode(np.frombuffer(f.read(), np.uint8), cv2.IMREAD_COLOR)

    template = await asyncio.to_thread(_load_template)
    flag = False

    # 循环点击模板匹配到的按钮，直到它在屏幕上消失（最多 20 次，防止死循环）
    for _ in range(20):
        try:
            screenshot_bytes = await page.screenshot(
                timeout=10000, animations="disabled"
            )
        except Exception as e:
            # 截图超时/失败：当作按钮已消失，跳过本次验证，不让异常崩掉整个流程
            print(f"  [验证] 截图失败，跳过本次验证: {e}")
            break
        matched, click_x, click_y = await asyncio.to_thread(
            _match_template, screenshot_bytes, template
        )
        if not matched:
            break
        flag = True
        await page.mouse.click(click_x, click_y)
        await page.wait_for_timeout(1000)  # 等页面刷新后重新截图判断

    OTP = page.locator("//input[@name='otpDeviceContext']").last
    if await OTP.count() > 0:
        flag = True

    if flag:
        await page.wait_for_load_state(state="load", timeout=60000)
        await OTP.wait_for(state="visible", timeout=60000)
        await OTP.click(force=True, timeout=5000)

        signin_button = page.locator("//span[@id='auth-send-code-announce']")
        await signin_button.click(force=True, timeout=60000)

        verified_text = page.get_by_role("textbox", name="输入验证码：")
        await page.wait_for_timeout(5000)
        count = await verified_text.count()
        value = (await verified_text.input_value()).strip()
        print(count)
        print(value)
        if count > 0 and value:
            await page.get_by_role("button", name="登录").click(force=True, timeout=6000)


# ==================== 线程池调度 ====================

def use_all_browser_run_task_with_thread_pool(browser_list, max_threads=3):
    """
    使用线程池控制最大并发浏览器数；单店铺失败不影响其它店铺（按 future 收集结果）。

    :param browser_list: 店铺列表
    :param max_threads: 最大并发线程数
    """
    result_list = []
    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = [executor.submit(process_one_browser, b) for b in browser_list]
        for f in futures:
            try:
                result_list.append(f.result())
            except Exception as exc:  # noqa: BLE001
                result_list.append({"status": "error", "message": f"店铺处理异常: {exc}"})
    return result_list


# ==================== 同步：领星ERP库存下载 ====================

def get_lingxing_excel():
    """登录领星ERP导出最新FBA库存Excel到 config.TRACK_TABLE_INVENTORY_SAVE_PATH，返回保存路径；失败返回 None。"""
    global _LINGXING_LAST_ERROR
    _LINGXING_LAST_ERROR = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--incognito',
                    '--disable-gpu',  # 禁用GPU
                    '--no-sandbox',  # 安全沙箱关闭
                    '--disable-dev-shm-usage',  # 降低内存
                    '--disable-images',  # 禁用图片（可选，超级省资源）
                ],
            )
            browser_context = browser.new_context()
            page = browser_context.new_page()

            page.goto("https://erp.lingxing.com/erp/listing")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            while page.get_by_role("textbox", name="手机号/用户名/邮箱").count() > 0:
                try:
                    page.wait_for_load_state("domcontentloaded")
                    page.get_by_role("textbox", name="手机号/用户名/邮箱").fill(config.LINGXING_USERNAME)
                    page.get_by_role("textbox", name="密码").fill(config.LINGXING_PASSWORD)
                    page.get_by_role("button", name="登录").click(force=True, timeout=60000)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

            if page.get_by_role("button", name="Close").count() > 0:
                page.get_by_role("button", name="Close").click(force=True, timeout=60000)
                page.wait_for_timeout(1000)
                page.get_by_role("button", name="跳过，暂不绑定").click(force=True, timeout=60000)
                page.wait_for_timeout(1000)
                page.get_by_role("button", name="完成登录").click(force=True, timeout=60000)
                page.wait_for_timeout(2000)
            logger.info("登录领星账号成功")
            try:
                page.wait_for_load_state("networkidle", timeout=60000)
            except Exception:
                pass

            page.goto("https://erp.lingxing.com/erp/muser/downloadCenter")

            tr = page.locator("//*[@id='ak-table-list']/div[1]/div/div[2]/div[1]/div[2]/table/tbody/tr[contains(.,'定时生成')][contains(.,'FBA仓库')][1]")

            last_td = tr.locator("td").last
            last_td.filter(has_text="下载").wait_for(state="visible", timeout=60000)

            with page.expect_download(timeout=60000) as download_info:
                last_td.filter(has_text="下载").click(force=True, timeout=60000)

            download = download_info.value
            download_path = config.TRACK_TABLE_INVENTORY_SAVE_PATH
            download.save_as(download_path)
            logger.info("导出成功，领星excel文件已保存 %s", download_path)
            return str(download_path)
    except Exception as e:
        # 用日志而不是 print：工具走 MCP stdio 传输时 stdout 是协议通道，
        # print 既看不到又可能污染 JSON-RPC 流。日志落到 stderr/后端日志。
        logger.warning("自动获取领星excel文件失败: %s", e)
        _LINGXING_LAST_ERROR = f"{type(e).__name__}: {e}"
        return None


# ==================== 同步：合同到港时间填充（源文件按店铺） ====================

def fill_arrival_time_from_contract(source_path, contract_path):
    """
    从本店铺轨迹跟踪表读取数据，过滤已完成/拦截/已退运状态，
    按站点和出运合同号分组，对于到港时间为空的出运合同号，
    从订单合同汇总表中查找并填充到港时间，最后写回原文件。

    :param source_path:  本店铺轨迹跟踪表路径（= {outDir}/{标签}轨迹跟踪表.xlsx）
    :param contract_path: 订单合同汇总表路径（config.TRACK_TABLE_CONTRACT_PATH）
    """
    # ========== 1. 读取源文件所有Sheet ==========
    print(f"读取源文件: {source_path}")
    all_sheets = pd.read_excel(source_path, sheet_name=None, engine="openpyxl")

    # ========== 2. 读取订单合同汇总表（分站点构建查找表） ==========
    print(f"读取合同汇总表: {contract_path}")
    contract_sheets = pd.read_excel(contract_path, sheet_name=None, engine="openpyxl")
    print(f"合同汇总表Sheet: {list(contract_sheets.keys())}")

    def _build_lookup(sheet_df):
        """从单个Sheet的DataFrame构建 出运合同号 -> 到港时间 的映射"""
        if '出运合同号' not in sheet_df.columns or '到港时间' not in sheet_df.columns:
            print(f"  警告: 未找到'出运合同号'或'到港时间'列，列名: {list(sheet_df.columns)}")
            return {}
        lookup_df = sheet_df[['出运合同号', '到港时间']].copy()
        # 关键：先 ffill 出运合同号确定分组，再按分组各自 ffill 到港时间
        # 否则先 ffill 到港时间会导致前一个合同的到港时间串到后一个合同
        lookup_df['出运合同号'] = lookup_df['出运合同号'].ffill()
        lookup_df['到港时间'] = lookup_df.groupby('出运合同号')['到港时间'].ffill()
        # 每个出运合同号取第一条非空到港时间
        lookup_df = lookup_df.dropna(subset=['到港时间'])
        lookup_df = lookup_df.drop_duplicates(subset=['出运合同号'], keep='first')
        lookup_df = lookup_df.dropna(subset=['出运合同号'])
        lookup_df['到港时间'] = pd.to_datetime(lookup_df['到港时间'], errors='coerce').dt.strftime('%Y-%m-%d')
        lookup_df = lookup_df.dropna(subset=['到港时间'])
        return lookup_df.set_index('出运合同号')['到港时间'].to_dict()

    # 美国站点 -> 出运数据明细-US
    lookup_us = {}
    if '出运数据明细-US' in contract_sheets:
        lookup_us = _build_lookup(contract_sheets['出运数据明细-US'])
        print(f"美国查找表(出运数据明细-US): {len(lookup_us)} 条记录")
    else:
        print("警告: 未找到Sheet '出运数据明细-US'")

    # 加拿大站点 -> 出运数据明细-CA
    lookup_ca = {}
    if '出运数据明细-CA' in contract_sheets:
        lookup_ca = _build_lookup(contract_sheets['出运数据明细-CA'])
        print(f"加拿大查找表(出运数据明细-CA): {len(lookup_ca)} 条记录")
    else:
        print("警告: 未找到Sheet '出运数据明细-CA'")

    # ========== 3. 合并所有Sheet，按国家分组处理 ==========
    # 给每个Sheet添加"来源Sheet"列，合并后按国家分组
    frames = []
    for sheet_name, df in all_sheets.items():
        df = df.copy()
        df['来源Sheet'] = sheet_name
        frames.append(df)

    combined_df = pd.concat(frames, ignore_index=True)
    print(f"\n合并后总行数: {len(combined_df)}")

    # 3a. 标记需要跳过的已完成/拦截/已退运/已取消状态（不删除，保留写入最终表格）
    skipped_count = 0
    if '状态' in combined_df.columns:
        skip_mask = combined_df['状态'].astype(str).str.contains(SKIP_STATUS_PATTERN, na=False)
        skipped_count = skip_mask.sum()
        if skipped_count > 0:
            print(f"检测到 {skipped_count} 行已完成/拦截/已退运/已取消状态（将跳过查询但保留写入）")
    else:
        print("警告: 未找到'状态'列")

    # 3b. 统一站点名称（"加拿大站" → "加拿大"，"美国站" → "美国"）
    def _normalize_site(site_val):
        s = str(site_val).strip()
        if '加拿大' in s or 'CA' in s.upper():
            return '加拿大'
        elif '美国' in s or 'US' in s.upper():
            return '美国'
        return s

    combined_df['站点'] = combined_df['站点'].apply(_normalize_site)
    print(f"站点归一化后: {combined_df['站点'].unique().tolist()}")

    # 3c. 按国家（站点）分组
    if '出运合同号' not in combined_df.columns:
        print("警告: 未找到'出运合同号'列，返回空结果")
        return {}

    filled_count_total = 0
    output_by_country = {}

    for site, site_group in combined_df.groupby('站点'):
        print(f"\n--- 站点: {site} (共 {len(site_group)} 行) ---")

        # 根据站点选择对应的查找表
        if '加拿大' in str(site):
            active_lookup = lookup_ca
        else:
            active_lookup = lookup_us  # 默认走美国

        site_df = site_group.copy()

        # 再按出运合同号分组
        for contract_no, contract_group in site_df.groupby('出运合同号'):
            # 检查该出运合同号对应的到港时间是否为空
            arrival_values = contract_group['到港时间']
            is_empty = (
                arrival_values.isna()
                | (arrival_values.astype(str).str.strip() == '')
                | (arrival_values.astype(str).str.strip() == 'NaT')
                | (arrival_values.astype(str).str.strip() == 'nan')
            )

            if is_empty.any():
                contract_no_str = str(contract_no).strip()
                if contract_no_str in active_lookup:
                    filled_arrival = active_lookup[contract_no_str]
                    row_indices = site_df[
                        site_df['出运合同号'] == contract_no
                    ].index
                    for idx in row_indices:
                        val = site_df.at[idx, '到港时间']
                        if pd.isna(val) or str(val).strip() in ('', 'NaT', 'nan'):
                            site_df.at[idx, '到港时间'] = filled_arrival
                            filled_count_total += 1
                    print(f"  出运合同号 {contract_no_str}: 填充到港时间 -> {filled_arrival}")
                else:
                    print(f"  出运合同号 {contract_no_str}: 在合同汇总表中未找到")

        # 按 (来源Sheet, 货件编码) 联合分组，避免不同Sheet的同名货件被合并
        sheet_shipment_groups = {}
        for (src_sheet, shipment_no), shipment_group in site_df.groupby(['来源Sheet', '货件编码']):
            sheet_shipment_groups.setdefault(src_sheet, {})[shipment_no] = shipment_group

        output_by_country[site] = sheet_shipment_groups

        unique_shipment_count = sum(len(sd) for sd in sheet_shipment_groups.values())
        print(f"  站点 [{site}] 唯一货件编码数: {unique_shipment_count}")

    # ========== 4. 重构为按Sheet分组 → 返回 ==========
    output_by_sheet = {}
    for country, sheet_dict in output_by_country.items():
        for src_sheet, shipment_dict in sheet_dict.items():
            for shipment_no, df in shipment_dict.items():
                output_by_sheet.setdefault(src_sheet, {}).setdefault(country, {})[shipment_no] = df

    print(f"\n===== 共填充 {filled_count_total} 条到港时间 =====")
    for sheet_name, country_dict in output_by_sheet.items():
        total = sum(len(sd) for sd in country_dict.values())
        print(f"  Sheet [{sheet_name}]: {len(country_dict)} 个国家, {total} 个货件编码")
    return output_by_sheet


# ==================== 任务入口 ====================

def run(payload: dict) -> dict:
    """更新亚马逊轨迹跟踪表，返回 {status, data, message}。

    payload:
        store: str  店铺标签（'巧逗豆'/'天安'），为空则处理 TRACK_TABLE_STORES 中全部店铺
    """
    global _INVENTORY_PATH
    try:
        store_label = (payload.get("store") or "").strip()
        stores = config.TRACK_TABLE_STORES
        if not stores:
            raise RuntimeError("TRACK_TABLE_STORES 未设置，无法定位店铺配置")

        if store_label:
            if store_label not in stores:
                raise RuntimeError(f"未找到店铺配置: {store_label}，可用: {list(stores.keys())}")
            selected = {store_label: stores[store_label]}
        else:
            selected = stores

        # ---- 配置完整性校验（不触发任何真实业务） ----
        for label, cfg in selected.items():
            if not cfg.get("browserOauth"):
                raise RuntimeError(f"店铺 [{label}] 缺少 browserOauth")
            if not cfg.get("outDir"):
                raise RuntimeError(f"店铺 [{label}] 缺少 outDir")
        if not config.LINGXING_USERNAME or not config.LINGXING_PASSWORD:
            raise RuntimeError("LINGXING_USERNAME / LINGXING_PASSWORD 未设置，无法下载领星库存")
        if not config.TRACK_TABLE_INVENTORY_SAVE_PATH:
            raise RuntimeError("TRACK_TABLE_INVENTORY_SAVE_PATH 未设置，无法保存领星库存")
        if not config.TRACK_TABLE_PASSKEY_TEMPLATE:
            raise RuntimeError("TRACK_TABLE_PASSKEY_TEMPLATE 未设置，无法处理验证码")
        if not config.TRACK_TABLE_CONTRACT_PATH:
            raise RuntimeError("TRACK_TABLE_CONTRACT_PATH 未设置，无法填充到港时间")

        # ---- 领星下载最新FBA库存（完整闭环） ----
        logger.info("下载领星FBA库存")
        inventory_path = get_lingxing_excel()
        if not inventory_path:
            detail = f"：{_LINGXING_LAST_ERROR}" if _LINGXING_LAST_ERROR else ""
            raise RuntimeError(f"领星ERP FBA库存下载失败{detail}")
        _INVENTORY_PATH = inventory_path

        browser_list = []
        for label, cfg in selected.items():
            out_dir = cfg["outDir"]
            browser_list.append({
                "label": label,
                "browserName": cfg.get("browserName", label),
                "browserOauth": cfg["browserOauth"],
                "outDir": out_dir,
                "outPath": os.path.join(out_dir, f"{label}轨迹跟踪表.xlsx"),
                "logPath": os.path.join(out_dir, f"{label}_处理日志.txt"),
                "inventory": cfg.get("inventory", {}),
            })

        with ZINIAO_LOCK:  # 串行化对紫鸟客户端的访问
            try:
                print("=====终止紫鸟客户端=====")
                kill_process("v6")
                print("=====启动客户端=====")
                start_browser()
                print("=====更新内核=====")
                update_core()

                results = use_all_browser_run_task_with_thread_pool(
                    browser_list, max_threads=min(3, len(browser_list))
                )
            finally:
                print("=====关闭客户端=====")
                try:
                    get_exit()
                except Exception:  # noqa: BLE001
                    pass

        failed = [r for r in results if r.get("status") != "success"]
        ok = [r.get("label", b["label"]) for r, b in zip(results, browser_list)
              if r.get("status") == "success"]
        if len(failed) == len(results):
            raise RuntimeError(f"全部店铺处理失败: {failed}")

        return {
            "status": "success" if not failed else "partial",
            "data": {
                "stores": ok,
                "failed": [f.get("message", f) for f in failed],
                "inventory_path": inventory_path,
                "message": f"完成：成功 {len(ok)}/{len(results)} 个店铺{f'，失败 {len(failed)} 个' if failed else ''}",
            },
            "message": "",
        }
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "data": "", "message": f"轨迹跟踪表更新失败: {exc}"}

if __name__ == "__main__":
    run({"store": "巧逗豆"})