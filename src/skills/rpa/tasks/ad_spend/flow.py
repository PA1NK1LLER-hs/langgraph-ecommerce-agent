# -*- coding: utf-8 -*-
"""广告花费查询流程 — 多日期 + 断点续写。

从 `ExpenseVerification/广告查询/ziniao_playwright_http_py3_v2.py` 迁移，
硬编码路径/凭证/店铺/日期全部改为由 config + payload 传入，
脚本里的 exit() 改为抛异常，由 run() 统一归一化为 {status, data, message}。
"""

import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from playwright.sync_api import sync_playwright

from skills.rpa.common import config
from skills.rpa.common.ziniao_client import (
    ZINIAO_LOCK,
    check_verified_status,
    close_store,
    get_browser_context,
    get_exit,
    kill_process,
    open_ip_check,
    open_launcher_page,
    open_store,
    start_browser,
    update_core,
)
from skills.rpa.common.checkpoint import (
    format_checkpoint_summary,
    get_checkpoint_completed_dates,
    load_checkpoint,
    save_checkpoint,
)
from skills.rpa.common.excel_io import (
    TEMPLATE_BASENAME,
    map_site_results_to_operators,
    read_excel_to_nested_dict,
    write_result_to_excel,
)

CHECKPOINT_FILENAME = "checkpoint_ads.json"


def build_country_products(original_result: dict) -> dict:
    """将 sheet->站点->关键词 的模板结构去重合并成 站点->商品集合。"""
    country_products: dict = {}
    for _operator, sites in original_result.items():
        for site, products in sites.items():
            if site not in country_products:
                country_products[site] = set()
            for p in products:
                if p and p != "/":
                    country_products[site].add(p.strip())
    return country_products


def build_store_tasks(country_products: dict) -> list:
    """把国家-商品按「美国本土 / 其它」拆到两个店铺任务。"""
    normal = {c: p for c, p in country_products.items() if c != "美国本土店铺"}
    us_local = {c: p for c, p in country_products.items() if c == "美国本土店铺"}

    stores = config.AD_SPEND_STORES
    tasks = []
    for kind, bucket in (("normal", normal), ("us_local", us_local)):
        cfg = stores.get(kind)
        if bucket and cfg:
            tasks.append({
                "browserName": cfg.get("browserName"),
                "browserOauth": cfg.get("browserOauth"),
                "country_products": bucket,
            })
    return tasks


def use_one_browser_run_task_v2(browser: dict, dates_to_query: list,
                                checkpoint_data: dict | None = None,
                                checkpoint_file: str | None = None,
                                on_date_complete=None) -> dict:
    """单店铺：打开一次，按国家设筛选，遍历日期逐商品查询，边查边写断点。

    每个商品查询后立即 save_checkpoint；每个日期完成后回调 on_date_complete 写 Excel。
    """
    if checkpoint_data is None:
        checkpoint_data = {}

    store_id = browser.get("browserOauth")
    store_name = browser.get("browserName")
    country_products = browser.get("country_products", {})

    ret_json = open_store(store_id)
    store_id = ret_json.get("browserOauth") or ret_json.get("browserId")

    site_date_results: dict = {}  # {(country, date_str): {product: spend}}

    with sync_playwright() as playwright:
        try:
            browser_context = get_browser_context(playwright, ret_json.get("debuggingPort"))
            if browser_context is None:
                close_store(store_id)
                return site_date_results

            ip_check_url = ret_json.get("ipDetectionPage")
            if not ip_check_url:
                raise RuntimeError("ipDetectionPage 为空，请升级紫鸟浏览器")

            if not open_ip_check(browser_context, ip_check_url):
                close_store(store_id)
                return site_date_results

            open_launcher_page(browser_context, ret_json.get("launcherPage"))
            time.sleep(2)

            # 导航到广告后台（整个店铺只做一次）
            page = browser_context.new_page()
            page.goto("https://advertising.amazon.com/campaign-manager", wait_until="load", timeout=60000)
            check_verified_status(page)
            page.wait_for_load_state("load", timeout=60000)
            body = page.locator("//div[@id='globalBetaAllCampaigns:table']")
            body.wait_for(state="visible", timeout=60000)
            time.sleep(2)

            for country_name, products in country_products.items():
                print(f"\n===== 国家: {country_name} | 商品数: {len(products)} | 日期数: {len(dates_to_query)} =====")

                # 清除旧筛选 + 设置国家筛选（每个国家一次）
                delete_btn = page.get_by_role("button", name="删除所有")
                for _ in range(20):
                    try:
                        if not delete_btn.is_visible():
                            break
                        delete_btn.scroll_into_view_if_needed()
                        delete_btn.click(force=True, timeout=5000)
                        time.sleep(1)
                        delete_btn = page.get_by_role("button", name="删除所有")
                    except Exception:
                        time.sleep(1)
                        delete_btn = page.get_by_role("button", name="删除所有")
                        if not delete_btn.is_visible():
                            break

                page.get_by_role("button", name="筛选条件").click(force=True, timeout=5000)
                page.wait_for_timeout(500)
                page.get_by_role("menuitem", name="国家/地区").click(force=True, timeout=5000)
                page.wait_for_timeout(500)
                page.wait_for_load_state("load", timeout=60000)

                if country_name == "美国本土店铺":
                    search_text = "美国"
                elif country_name == "澳洲站":
                    search_text = "澳大利亚"
                else:
                    search_text = country_name.replace("站", "")
                page.locator("label").filter(has_text=search_text).click()
                time.sleep(0.5)
                page.get_by_role("button", name="应用").click(force=True)
                page.wait_for_load_state("load", timeout=60000)
                time.sleep(1)

                for query_date in dates_to_query:
                    date_str = query_date.strftime("%Y-%m-%d")
                    print(f"\n  [{country_name}] 日期: {date_str}")

                    products_list = [p.strip() for p in sorted(products) if p and p != "/"]
                    done_in_checkpoint = sum(
                        1 for p in products_list if f"{country_name}|{date_str}|{p}" in checkpoint_data
                    )

                    if done_in_checkpoint == len(products_list) and products_list:
                        site_date_results[(country_name, date_str)] = {
                            p: checkpoint_data.get(f"{country_name}|{date_str}|{p}")
                            for p in products_list
                        }
                        if on_date_complete:
                            on_date_complete(country_name, date_str, site_date_results[(country_name, date_str)])
                        continue

                    max_date_retries = 3
                    retry_count = 0
                    date_success = False

                    while retry_count < max_date_retries:
                        try:
                            page.wait_for_load_state("load", timeout=60000)
                            retry = 0
                            while page.get_by_role("option", name="昨天").count() <= 0 and retry < 3:
                                try:
                                    page.locator(
                                        '//button[@id="UCM-CM-APP:globalBetaAllCampaigns:dateRangeFilter:openContainer" or '
                                        '@id="globalBetaAllCampaigns:dateRangeFilter:openContainer"]'
                                    ).click(force=True, timeout=60000)
                                    page.get_by_role("button", name="转至上个月").click(force=True, timeout=5000)
                                    page.locator(f"//button[@data-iso-date='{date_str}']").click(force=True, timeout=5000)
                                    break
                                except Exception:
                                    try:
                                        page.locator(f"//button[@data-iso-date='{date_str}']").click(force=True, timeout=5000)
                                    except Exception:
                                        page.reload()
                                        body.wait_for(state="visible", timeout=60000)
                                        time.sleep(0.5)
                                    retry += 1

                            page.get_by_role("button", name="应用").click()
                            page.wait_for_load_state("load", timeout=60000)
                            time.sleep(1.5)

                            site_date_results[(country_name, date_str)] = {}

                            for product in sorted(products):
                                if not product or product == "/":
                                    continue
                                product = product.strip()
                                key = f"{country_name}|{date_str}|{product}"
                                if key in checkpoint_data:
                                    site_date_results[(country_name, date_str)][product] = checkpoint_data[key]
                                    continue

                                try:
                                    search_box = page.get_by_role("searchbox", name="Search")
                                    search_box.click(force=True, timeout=6000)
                                    search_box.fill(product)
                                    search_box.press("Enter")
                                    time.sleep(1)

                                    page.locator(
                                        '[id="UCM-CM-APP:globalBetaAllCampaigns:overlay:loading"] h4'
                                    ).filter(has_text="正在加载").wait_for(state="hidden", timeout=60000)

                                    page.locator(
                                        f"//span[@class='cell-renderer-content-text' and contains(text(), '{product}')]"
                                    ).first.wait_for(state="visible", timeout=60000)

                                    cell = page.locator(
                                        "(//div[@role='presentation']//div[@col-id='spend'])[last()]")
                                    page.evaluate("""
                                        var c = document.querySelector('.ag-body-horizontal-scroll-viewport');
                                        if (c) c.scrollLeft = c.scrollWidth;
                                    """)
                                    time.sleep(0.5)
                                    try:
                                        cell.wait_for(state="visible", timeout=10000)
                                    except Exception:
                                        pass

                                    if cell.count() > 0:
                                        time.sleep(0.5)
                                        spend_val = cell.first.text_content().replace("US$", "")
                                    else:
                                        spend_val = None

                                    site_date_results[(country_name, date_str)][product] = spend_val
                                    checkpoint_data[key] = spend_val if spend_val is not None else ""
                                    if checkpoint_file:
                                        save_checkpoint(checkpoint_file, checkpoint_data)
                                except Exception as e:
                                    print(f"      {country_name}-{product}: 查询异常 {e}")
                                    site_date_results[(country_name, date_str)][product] = None
                                    checkpoint_data[key] = ""
                                    if checkpoint_file:
                                        save_checkpoint(checkpoint_file, checkpoint_data)

                            date_success = True
                            break

                        except Exception as e:
                            retry_count += 1
                            print(f"    [{country_name}] {date_str}: 第{retry_count}/{max_date_retries}次重试, 异常: {e}")
                            if retry_count >= max_date_retries:
                                restored = {}
                                for p in products_list:
                                    key = f"{country_name}|{date_str}|{p}"
                                    if key in checkpoint_data:
                                        restored[p] = checkpoint_data[key]
                                if restored:
                                    site_date_results[(country_name, date_str)] = restored
                                elif (country_name, date_str) not in site_date_results:
                                    site_date_results[(country_name, date_str)] = {}
                            else:
                                time.sleep(3)

                    if on_date_complete and (country_name, date_str) in site_date_results:
                        date_data = site_date_results[(country_name, date_str)]
                        if date_data:
                            try:
                                on_date_complete(country_name, date_str, date_data)
                            except Exception as cb_err:
                                print(f"    ⚠ 日期完成回调异常: {cb_err}")

        except Exception:
            print("脚本运行异常:" + traceback.format_exc())
            return site_date_results
        finally:
            print(f"=====关闭店铺：{store_name}=====")
            try:
                close_store(store_id)
            except Exception:
                pass

    return site_date_results


def _write_checkpoint_dates(checkpoint_data: dict, store_tasks: list, d: date,
                            output_dir: str, original_result: dict) -> None:
    """把断点全覆盖的某一天直接从 JSON 写入 Excel（不开浏览器）。"""
    ds = d.strftime("%Y-%m-%d")
    date_slice: dict = {}
    for task in store_tasks:
        for country, products in task.get("country_products", {}).items():
            date_results = {}
            for p in products:
                if p and p != "/":
                    key = f"{country}|{ds}|{p.strip()}"
                    if key in checkpoint_data:
                        date_results[p.strip()] = checkpoint_data[key]
            if date_results:
                for op, sites in original_result.items():
                    if country in sites:
                        date_slice.setdefault(op, {})[country] = date_results
    if date_slice:
        write_result_to_excel(date_slice, output_dir, d)


def run(payload: dict) -> dict:
    """执行广告花费查询，返回 {status, data, message}。

    payload:
        start_date: str  起始日期 YYYY-MM-DD
        end_date:   str  结束日期（可选，默认 = start_date）
        output_dir: str  输出目录（可选，默认 config.AD_SPEND_OUTPUT_DIR）
    """
    try:
        start_date = (payload.get("start_date") or "").strip()
        end_date = (payload.get("end_date") or "").strip() or start_date
        output_dir = (payload.get("output_dir") or "").strip() or config.AD_SPEND_OUTPUT_DIR

        if not start_date:
            raise RuntimeError("start_date 未提供，格式 YYYY-MM-DD")
        if not output_dir:
            raise RuntimeError("AD_SPEND_OUTPUT_DIR 未设置，无法定位广告花费报表目录")

        template_file = os.path.join(output_dir, TEMPLATE_BASENAME)
        checkpoint_file = os.path.join(output_dir, CHECKPOINT_FILENAME)

        with ZINIAO_LOCK:  # 串行化对紫鸟客户端的访问
            kill_process("v6")
            start_browser()
            update_core()

            checkpoint_data = load_checkpoint(checkpoint_file)
            if checkpoint_data:
                print(format_checkpoint_summary(checkpoint_data))

            original_result = read_excel_to_nested_dict(template_file)
            country_products = build_country_products(original_result)
            store_tasks = build_store_tasks(country_products)

            d0 = datetime.strptime(start_date, "%Y-%m-%d").date()
            d1 = datetime.strptime(end_date, "%Y-%m-%d").date()
            if d1 < d0:
                raise RuntimeError("end_date 不能早于 start_date")
            all_dates = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]

            fully_done = get_checkpoint_completed_dates(checkpoint_data, store_tasks, all_dates)
            dates_to_query = [d for d in all_dates if d not in fully_done]

            # 断点全覆盖日期直接写 Excel
            for d in sorted(fully_done):
                _write_checkpoint_dates(checkpoint_data, store_tasks, d, output_dir, original_result)

            if not dates_to_query:
                get_exit()
                return {
                    "status": "success",
                    "data": {"message": "所有日期断点全覆盖，无需浏览器查询", "completed_dates": len(fully_done)},
                    "message": "",
                }

            def make_on_date_complete():
                def on_date_complete(country_name, date_str, date_results):
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                    date_slice = {}
                    for op, sites in original_result.items():
                        if country_name in sites:
                            date_slice.setdefault(op, {})[country_name] = date_results
                    if date_slice:
                        try:
                            write_result_to_excel(date_slice, output_dir, date_obj)
                        except Exception as e:
                            print(f"  ⚠ 写入 Excel 失败 [{country_name} {date_str}]: {e}")
                return on_date_complete

            on_date_complete = make_on_date_complete()

            all_site_results: dict = {}
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(use_one_browser_run_task_v2, t, dates_to_query,
                                           checkpoint_data, checkpoint_file, on_date_complete)
                           for t in store_tasks]
                for f in futures:
                    try:
                        r = f.result()
                        if r:
                            all_site_results.update(r)
                    except Exception as e:
                        print(f"店铺任务异常: {e}")

            # 兜底写入：确保所有待查日期都已落盘
            operator_results = map_site_results_to_operators(all_site_results, original_result)
            for d in dates_to_query:
                ds = d.strftime("%Y-%m-%d")
                date_slice = {}
                for op, sites in operator_results.items():
                    for site, dates in sites.items():
                        if ds in dates:
                            date_slice.setdefault(op, {})[site] = dates[ds]
                if date_slice:
                    write_result_to_excel(date_slice, output_dir, d)

            get_exit()
            return {
                "status": "success",
                "data": {
                    "queried_dates": len(dates_to_query),
                    "checkpoint_dates": len(fully_done),
                    "output_dir": output_dir,
                    "message": f"完成：待查 {len(dates_to_query)} 天，断点恢复 {len(fully_done)} 天",
                },
                "message": "",
            }
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "data": "", "message": f"广告花费查询失败: {exc}"}
