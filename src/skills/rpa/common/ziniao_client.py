# -*- coding: utf-8 -*-
"""紫鸟浏览器 HTTP 客户端 + 生命周期。

全项目唯一的紫鸟底层实现。所有 RPA（交互式原子操作 + 批量流程）都通过
本模块访问紫鸟，不再各自 copy 一份 send_http / open_store。

约定：本模块只负责「怎么跟紫鸟通信」，不含业务逻辑；并且**绝不 exit()**，
失败统一抛 RuntimeError，由上层工具决定如何呈现给 Agent。
"""

import json
import subprocess
import threading
import time
import uuid

import requests

from . import config

# 全局互斥锁：交互式原语与批量流程共用同一个紫鸟客户端端口，
# 串行化对紫鸟的访问，避免并发抢占同一端口/客户端。
ZINIAO_LOCK = threading.Lock()


def send_http(data: dict) -> dict | None:
    """向紫鸟 HTTP 端口发送请求，失败时返回 None 或 {"error": ...}。"""
    url = f"http://127.0.0.1:{config.ZINIAO_SOCKET_PORT}"
    try:
        resp = requests.post(url, json.dumps(data).encode("utf-8"), timeout=120)
        return json.loads(resp.text)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def kill_process(version: str = "v6") -> None:
    """终止紫鸟浏览器主进程。启动新任务前调用，避免端口冲突。"""
    if not config.IS_WINDOWS:
        return
    process_name = "SuperBrowser.exe" if version == "v5" else "ziniao.exe"
    subprocess.run(
        ["taskkill", "/f", "/t", "/im", process_name],
        capture_output=True, timeout=10,
    )
    time.sleep(3)


def start_browser() -> None:
    """启动紫鸟客户端（web_driver HTTP 模式）。"""
    cmd = [
        config.ZINIAO_CLIENT_PATH,
        "--run_type=web_driver",
        "--ipc_type=http",
        f"--port={config.ZINIAO_SOCKET_PORT}",
    ]
    if not config.IS_WINDOWS:
        cmd.insert(1, "--no-sandbox")
    try:
        subprocess.Popen(cmd)
        time.sleep(5)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"启动紫鸟客户端失败: {exc}") from exc


def update_core(timeout: int = 120) -> dict:
    """下载浏览器内核（打开店铺前调用）。循环等待直至完成或超时。"""
    data = {"action": "updateCore", "requestId": str(uuid.uuid4())}
    data.update(config.ziniao_user())

    start = time.time()
    while time.time() - start < timeout:
        result = send_http(data)
        if result is None:
            time.sleep(2)
            continue
        if result.get("statusCode") == 0:
            return {"status": "success", "message": "内核更新完成"}
        if result.get("statusCode") == -10003:
            return {"status": "error", "message": "当前版本不支持此接口，请升级客户端"}
        time.sleep(2)
    return {"status": "error", "message": f"更新内核超时（{timeout}s）"}


def open_store(store_id: str, *, is_headless: int = 0, cookie_save: int = 0,
               is_privacy: int = 0, js_info: str = "") -> dict:
    """打开店铺，返回紫鸟原始响应（含 browserOauth/debuggingPort/ipDetectionPage 等）。"""
    data = {
        "action": "startBrowser",
        "isWaitPluginUpdate": 0,
        "isHeadless": is_headless,
        "requestId": str(uuid.uuid4()),
        "isWebDriverReadOnlyMode": 0,
        "cookieTypeLoad": 0,
        "cookieTypeSave": cookie_save,
        "runMode": "1",
        "isLoadUserPlugin": False,
        "pluginIdType": 1,
        "privacyMode": is_privacy,
    }
    data.update(config.ziniao_user())

    if str(store_id).isdigit():
        data["browserId"] = store_id
    else:
        data["browserOauth"] = store_id
    if len(str(js_info)) > 2:
        data["injectJsInfo"] = json.dumps(js_info)

    r = send_http(data)
    if r is None:
        raise RuntimeError("无法连接紫鸟客户端，请确认已启动")
    if str(r.get("statusCode")) != "0":
        raise RuntimeError(f"打开店铺失败: {json.dumps(r, ensure_ascii=False)}")
    return r


def close_store(browser_oauth: str) -> dict:
    """关闭指定店铺。"""
    data = {
        "action": "stopBrowser",
        "requestId": str(uuid.uuid4()),
        "duplicate": 0,
        "browserOauth": browser_oauth,
    }
    data.update(config.ziniao_user())
    r = send_http(data)
    if r is None:
        return {"status": "success", "message": "紫鸟无响应，已跳过关闭"}
    if str(r.get("statusCode")) != "0":
        raise RuntimeError(f"关闭店铺失败: {json.dumps(r, ensure_ascii=False)}")
    return r


def get_browser_list() -> list:
    """获取账号下所有店铺列表。"""
    data = {"action": "getBrowserList", "requestId": str(uuid.uuid4())}
    data.update(config.ziniao_user())
    r = send_http(data)
    if r is None:
        raise RuntimeError("无法连接紫鸟客户端")
    if str(r.get("statusCode")) != "0":
        raise RuntimeError(f"获取店铺列表失败: {json.dumps(r, ensure_ascii=False)}")
    return r.get("browserList", [])


def get_browser_context(playwright, port: int):
    """通过 CDP 连接已打开的店铺浏览器，返回其第一个 context。"""
    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    return browser.contexts[0]


def open_ip_check(browser_context, ip_check_url: str) -> bool:
    """打开 IP 检测页，等待检测通过。"""
    from playwright.sync_api import TimeoutError as PwTimeout
    try:
        page = browser_context.pages[0]
        page.goto(ip_check_url)
        btn = page.locator('//button[contains(@class, "styles_btn--success")]')
        btn.wait_for(timeout=60000)
        return True
    except PwTimeout:
        return False
    except Exception:
        return False


def open_launcher_page(browser_context, launcher_page: str) -> None:
    """打开店铺平台主页并处理验证状态。"""
    page = browser_context.pages[0]
    page.goto(launcher_page)
    page.wait_for_load_state(state="load")
    check_verified_status(page)


def check_verified_status(page) -> bool:
    """处理 Amazon 验证状态（验证码 / 登录 / continue）。"""
    try:
        continue_btn = page.locator("span.a-button.a-button-primary.a-span12")
        while continue_btn.is_visible():
            try:
                continue_btn.click(force=True, timeout=5000)
            except Exception:
                pass
            continue_btn = page.locator("span.a-button.a-button-primary.a-span12")

        verify_code = page.frame_locator('iframe[src*="twostep.html"]').get_by_text("验证码获取成功")
        if verify_code.is_visible():
            page.get_by_role("button", name="登录").click(force=True, timeout=5000)

        signin_btn = page.locator("//span[@id='auth-signin-button']")
        if signin_btn.count() > 0:
            signin_btn.click(force=True, timeout=5000)
        return True
    except Exception:
        return False


def get_exit() -> None:
    """关闭紫鸟客户端。"""
    data = {"action": "exit", "requestId": str(uuid.uuid4())}
    data.update(config.ziniao_user())
    send_http(data)
