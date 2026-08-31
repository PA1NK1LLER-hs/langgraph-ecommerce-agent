# -*- coding: utf-8 -*-
"""Amazon 评论/星级采集流程。

从 `src/skills/rpa_amazon_get_review.py` 迁入，去掉 @tool 装饰器，
入口改为 run(payload) -> {status, data, message}。
"""

import asyncio
import multiprocessing
import os
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from skills.rpa.common import config

amazon_country_dict = {
    "美国": "https://www.amazon.com/",
    "澳洲": "https://www.amazon.com.au/",
    "英国": "https://www.amazon.co.uk/",
    "日本": "https://www.amazon.co.jp/",
    "新加坡": "https://www.amazon.com.sg/",
    "法国": "https://www.amazon.fr/",
    "德国": "https://www.amazon.de/",
    "意大利": "https://www.amazon.it/",
    "西班牙": "https://www.amazon.es/",
    "加拿大": "https://www.amazon.ca/",
    "墨西哥": "https://www.amazon.com.mx/",
    "巴西": "https://www.amazon.com.br/",
}

CONCURRENT_PAGES_PER_BROWSER = 1
counter = 0
counter_lock = threading.Lock()
failed_list: list = []
failed_list_lock = threading.Lock()
success_list: list = []
success_list_lock = threading.Lock()


async def clear_browser_cache(context, page):
    try:
        await context.clear_cookies()
        try:
            await page.evaluate("""() => {
                localStorage.clear();
                sessionStorage.clear();
                if (window.indexedDB) {
                    indexedDB.databases().then(dbs => {
                        dbs.forEach(db => indexedDB.deleteDatabase(db.name));
                    });
                }
                if ('caches' in window) {
                    caches.keys().then(names => {
                        names.forEach(name => caches.delete(name));
                    });
                }
            }""")
        except Exception:
            pass
        await page.reload(wait_until="domcontentloaded")
    except Exception:
        pass


async def check_verified_status(context, page):
    dogs_of_amazon_link = page.locator('a[href="/dogsofamazon"] img[id="d"]')
    wrong = page.get_by_text("处理您的请求时出错")
    if await dogs_of_amazon_link.is_visible() or await wrong.is_visible():
        await clear_browser_cache(context, page)
        return True
    continue_shopping_button = page.locator("span.a-button.a-button-primary.a-span12")
    while await continue_shopping_button.is_visible():
        try:
            await continue_shopping_button.click(force=True, timeout=5000)
        except Exception:
            pass
        continue_shopping_button = page.locator("span.a-button.a-button-primary.a-span12")


async def process_single_asin(asin_dict, browser, semaphore):
    from playwright_stealth.stealth import Stealth
    stealth = Stealth()

    async with semaphore:
        browser_context = await browser.new_context()
        page = await browser_context.new_page()

        asin = asin_dict["asin"]
        country = asin_dict["country"]
        url = f"{amazon_country_dict[country]}dp/{asin}"

        await stealth.apply_stealth_async(page)
        star = None
        review = None
        try:
            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            await check_verified_status(browser_context, page)
            await page.wait_for_load_state(state="domcontentloaded")

            stars = page.locator("#acrPopover > span > a > span").first
            reviews = page.locator("#acrCustomerReviewText").first
            await page.wait_for_selector("#acrPopover", state="visible", timeout=30000)

            if await stars.count() > 0:
                star = await stars.text_content()
            if await reviews.count() > 0:
                review = await reviews.text_content()
                review = re.sub(r"\D", "", review)

            with counter_lock:
                global counter
                counter += 1

            if review or star:
                with success_list_lock:
                    success_list.append({
                        "operator": asin_dict["operator"], "name": asin_dict["name"],
                        "asin": asin, "star": star, "review": review,
                        "country": country, "isSuccess": "成功",
                    })
            else:
                await page.screenshot(path=f"失败/{asin}.png")
                with failed_list_lock:
                    failed_list.append({
                        "operator": asin_dict["operator"], "name": asin_dict["name"],
                        "asin": asin, "star": star, "review": review,
                        "country": country, "isSuccess": "失败",
                    })
        except Exception:
            with failed_list_lock:
                failed_list.append({
                    "operator": asin_dict["operator"], "name": asin_dict["name"],
                    "asin": asin, "star": star, "review": review,
                    "country": country, "isSuccess": "失败",
                })
        finally:
            await page.close()
            await browser_context.close()


async def process_batch_in_browser(asin_batch, batch_num, headless):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--incognito",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-images",
            ],
        )
        semaphore = asyncio.Semaphore(CONCURRENT_PAGES_PER_BROWSER)
        tasks = [process_single_asin(asin_dict, browser, semaphore) for asin_dict in asin_batch]
        await asyncio.gather(*tasks)
        await browser.close()


def run_task(asin_batch, batch_num, headless):
    asyncio.run(process_batch_in_browser(asin_batch, batch_num, headless))


def read_asin_from_excel(excel_path):
    df = pd.read_excel(excel_path)
    data_list = []
    for _, row in df.iterrows():
        name = str(row["品名"]).strip() if pd.notna(row["品名"]) else ""
        asin = str(row["ASIN"]).strip() if pd.notna(row["ASIN"]) else ""
        country = str(row["国家"]).strip() if pd.notna(row["国家"]) else ""
        operator = str(row["负责人"]).strip() if pd.notna(row["负责人"]) else ""
        if asin and country and country in ("美国", "加拿大"):
            data_list.append({"asin": asin, "country": country, "name": name, "operator": operator})
    return data_list


def excute_asin(data_list, headless=True, max_workers=6):
    global counter
    counter = 0
    count = len(data_list)
    if count == 0:
        return

    batch_size = (count + max_workers - 1) // max_workers
    batches = [data_list[i:i + batch_size] for i in range(0, count, batch_size)]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_task, batch, i + 1, headless) for i, batch in enumerate(batches)]
        for f in futures:
            f.result()


def calculate_optimal_concurrent():
    logical_cores = multiprocessing.cpu_count()
    physical_cores = logical_cores // 2
    optimal_threads = int(physical_cores * 1.2)
    optimal_threads = max(3, min(optimal_threads, 8))

    if logical_cores <= 4:
        coroutines_per_browser = 2
    else:
        coroutines_per_browser = 3

    return optimal_threads, coroutines_per_browser


def run(payload: dict) -> dict:
    """采集 Amazon 评论/星级，返回 {status, data, message}。"""
    global CONCURRENT_PAGES_PER_BROWSER, failed_list, success_list
    failed_list = []
    success_list = []

    try:
        excel_path = (payload.get("excel_path") or "").strip() or config.AMAZON_REVIEW_EXCEL_PATH
        if not excel_path:
            raise RuntimeError("AMAZON_REVIEW_EXCEL_PATH 未设置，无法读取 ASIN 列表")

        optimal_workers, CONCURRENT_PAGES_PER_BROWSER = calculate_optimal_concurrent()
        data_list = read_asin_from_excel(excel_path)
        if not data_list:
            return {"status": "success", "data": {"message": "无待采集的 ASIN", "groups": {}}, "message": ""}

        excute_asin(data_list, headless=True, max_workers=optimal_workers)

        t = 0
        while (len(failed_list) > 5 or t == 0) and len(failed_list) > 0 and t < 10:
            t += 1
            temp_list = failed_list.copy()
            failed_list.clear()
            excute_asin(temp_list, headless=True, max_workers=optimal_workers)

        success_list.extend(failed_list)

        country_groups = {}
        for item in success_list:
            country = item.get("country", "未知国家")
            country_groups.setdefault(country, []).append(item)

        return {
            "status": "success",
            "data": {"groups": country_groups, "total": len(success_list)},
            "message": "",
        }
    except Exception as exc:
        return {"status": "error", "data": "", "message": f"Amazon 评论采集失败: {exc}"}
