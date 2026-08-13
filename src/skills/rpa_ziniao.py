"""紫鸟浏览器 RPA 技能 — Agent 可调用的 @tool 自动化工具。

通过紫鸟 HTTP API 管理浏览器生命周期，结合 Playwright 实现
Amazon 广告花费查询等电商自动化任务。
"""

import json
import os
import platform
import subprocess
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any

import requests
from pydantic import BaseModel, Field
from langchain_core.tools import tool

from skills.ziniao_playwright_http_py3 import (
    kill_process, start_browser, update_core,
    read_excel_to_nested_dict, excute_result, excute_result_with_retry,
    get_exit,
)

# ---------------------------------------------------------------------------
# 配置（可通过环境变量覆盖）
# ---------------------------------------------------------------------------

_is_windows = platform.system() == "Windows"

ZINIAO_CLIENT_PATH = os.getenv(
    "ZINIAO_CLIENT_PATH",
    R"D:\software\ziniao\ziniao.exe" if _is_windows else "ziniao",
)
ZINIAO_SOCKET_PORT = int(os.getenv("ZINIAO_SOCKET_PORT", "16851"))

_ziniao_company = os.getenv("ZINIAO_COMPANY")
_ziniao_username = os.getenv("ZINIAO_USERNAME")

def _get_ziniao_user():
    """延迟校验：仅在 RPA 工具实际调用时才要求环境变量已设置。"""
    if not _ziniao_company:
        raise RuntimeError("ZINIAO_COMPANY 未设置，紫鸟 RPA 无法运行")
    if not _ziniao_username:
        raise RuntimeError("ZINIAO_USERNAME 未设置，紫鸟 RPA 无法运行")
    return {
        "company": _ziniao_company,
        "username": _ziniao_username,
        "password": os.getenv("ZINIAO_PASSWORD", ""),
    }

ZINIAO_USER = None  # 延迟初始化，由 _get_ziniao_user() 在调用时校验

# 店铺配置（通过 ZINIAO_STORES 环境变量配置，格式: "name:id,name:id"）
def _parse_stores() -> dict[str, str]:
    raw = os.getenv("ZINIAO_STORES", "")
    if raw:
        stores = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if ":" in pair:
                name, sid = pair.split(":", 1)
                stores[name.strip()] = sid.strip()
        return stores
    return {}

STORES = _parse_stores()
ALLOWED_RPA_STORES = set(STORES.keys())


def _ziniao_port() -> int:
    return ZINIAO_SOCKET_PORT


def _send_http(data: dict) -> dict | None:
    try:
        url = f"http://127.0.0.1:{_ziniao_port()}"
        resp = requests.post(url, json.dumps(data).encode("utf-8"), timeout=120)
        return json.loads(resp.text)
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# 全局 Playwright 会话
# ---------------------------------------------------------------------------


@dataclass
class RpaSession:
    active: bool = False
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    browser_oauth: str = ""
    store_name: str = ""
    last_error: str = ""

    def reset(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright is not None:
                self.playwright.stop()
        except Exception:
            pass
        self.active = False
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.browser_oauth = ""
        self.store_name = ""
        self.last_error = ""

    def get_page(self) -> Any:
        if not self.active or self.page is None:
            return None
        return self.page


_rpa_session = RpaSession()


def _rpa_reset_session():
    _rpa_session.reset()


def _rpa_get_page() -> Any:
    return _rpa_session.get_page()


# ---------------------------------------------------------------------------
# Pydantic args_schema 模型（确保 LLM 看到每个参数的 description）
# ---------------------------------------------------------------------------

class KillProcessArgs(BaseModel):
    version: str = Field(default="v6", description="客户端版本 'v5' 或 'v6'")


class UpdateCoreArgs(BaseModel):
    timeout: int = Field(default=120, description="超时秒数（默认 120）")


class CloseStoreArgs(BaseModel):
    browser_oauth: str = Field(default="", description="打开店铺时返回的 browserOauth。为空则用当前会话中的。")


class RpaClickArgs(BaseModel):
    text: str = Field(description="要点击的元素文本（如 '筛选条件'、'应用'、'昨天'）")
    index: int = Field(default=0, description="多匹配时选第几个（默认 0 即第一个）")


class RpaFillInputArgs(BaseModel):
    placeholder_or_name: str = Field(description="输入框的 placeholder 文本或 name 属性")
    value: str = Field(description="要填入的内容")


class RpaNavigateArgs(BaseModel):
    url: str = Field(description="目标 URL（如 'https://advertising.amazon.com/campaign-manager'）")


class RpaExtractTablesArgs(BaseModel):
    max_rows: int = Field(default=50, description="每个表格最大提取行数（默认 50）")


class RpaWaitArgs(BaseModel):
    seconds: int = Field(default=2, description="等待秒数（默认 2，最大 30）")


class RpaScrollArgs(BaseModel):
    direction: str = Field(default="down", description="滚动方向 — 'down'（向下）或 'up'（向上）")
    amount: int = Field(default=500, description="滚动像素数（默认 500）")


class RpaQueryCampaignSpendArgs(BaseModel):
    site_keywords_json: str = Field(
        default="",
        description='JSON 格式的 {"操作人":{"站点名": ["关键词1","关键词2"]}}。为空则从默认 Excel 读取。',
    )


# ---------------------------------------------------------------------------
# 浏览器生命周期
# ---------------------------------------------------------------------------

@tool(args_schema=KillProcessArgs)
def rpa_kill_process(version: str = "v6") -> dict[str, Any]:
    """终止紫鸟浏览器主进程。在启动新的 RPA 任务前调用，避免端口冲突。"""
    if not _is_windows:
        return {"status": "success", "message": "非 Windows 环境，跳过进程终止"}

    process_name = "SuperBrowser.exe" if version == "v5" else "ziniao.exe"
    try:
        subprocess.run(
            ["taskkill", "/f", "/t", "/im", process_name],
            capture_output=True, timeout=10,
        )
        time.sleep(3)
        return {"status": "success", "message": f"已终止 {process_name}"}
    except Exception as exc:
        return {"status": "error", "message": f"终止进程失败: {exc}"}


@tool
def rpa_start_browser() -> dict[str, Any]:
    """启动紫鸟浏览器客户端（web_driver HTTP 模式）。在开始任何店铺操作前必须调用，启动后需等待几秒让客户端就绪。"""
    port = _ziniao_port()
    if _is_windows:
        cmd = [
            ZINIAO_CLIENT_PATH,
            "--run_type=web_driver",
            "--ipc_type=http",
            f"--port={port}",
        ]
    else:
        cmd = [
            ZINIAO_CLIENT_PATH,
            "--no-sandbox",
            "--run_type=web_driver",
            "--ipc_type=http",
            f"--port={port}",
        ]

    try:
        subprocess.Popen(cmd)
        time.sleep(5)
        return {"status": "success", "message": "紫鸟客户端已启动", "port": port}
    except Exception as exc:
        return {"status": "error", "message": f"启动失败: {exc}"}


@tool(args_schema=UpdateCoreArgs)
def rpa_update_core(timeout: int = 120) -> dict[str, Any]:
    """下载浏览器内核（打开店铺前调用，需客户端 v5.285.7+）。会循环等待直至完成或超时。"""
    data = {
        "action": "updateCore",
        "requestId": str(uuid.uuid4()),
    }
    data.update(_get_ziniao_user())

    start = time.time()
    while time.time() - start < timeout:
        result = _send_http(data)
        if result is None:
            time.sleep(2)
            continue
        if result.get("statusCode") == 0:
            return {"status": "success", "message": "内核更新完成"}
        if result.get("statusCode") == -10003:
            return {"status": "error", "message": "当前版本不支持此接口，请升级客户端"}
        time.sleep(2)

    return {"status": "error", "message": f"更新内核超时（{timeout}s）"}


@tool
def rpa_exit_client() -> dict[str, Any]:
    """关闭紫鸟客户端。在所有 RPA 任务完成后调用，释放资源。"""
    data = {"action": "exit", "requestId": str(uuid.uuid4())}
    data.update(_get_ziniao_user())
    _send_http(data)
    return {"status": "success", "message": "已发送退出指令"}


# ---------------------------------------------------------------------------
# 店铺管理
# ---------------------------------------------------------------------------

@tool
def rpa_store_list() -> dict[str, Any]:
    """获取紫鸟账号下的所有店铺列表。返回每个店铺的名称和 browserOauth ID。调用前需确保紫鸟客户端已启动。"""
    data = {
        "action": "getBrowserList",
        "requestId": str(uuid.uuid4()),
    }
    data.update(_get_ziniao_user())
    r = _send_http(data)

    if r is None:
        return {"status": "error", "message": "无法连接紫鸟客户端，请确认已启动"}
    if str(r.get("statusCode")) == "0":
        stores = r.get("browserList", [])
        return {"status": "success", "stores": stores, "count": len(stores)}
    return {"status": "error", "message": f"获取店铺列表失败: {json.dumps(r, ensure_ascii=False)}"}


class OpenStoreArgs(BaseModel):
    store_id: str = Field(description="店铺 ID（browserOauth 或 browserId）")
    store_name: str = Field(default="", description="店铺名称（用于日志）")
    target_url: str = Field(
        default="https://advertising.amazon.com/campaign-manager",
        description="打开店铺后自动导航到的目标 URL",
    )
    is_headless: int = Field(default=0, description="是否无头模式（0=否, 1=是）")
    cookie_save: int = Field(default=0, description="是否保存 cookie（0=否, 1=是）")


@tool(args_schema=OpenStoreArgs)
def rpa_open_store(
    store_id: str,
    store_name: str = "",
    target_url: str = "https://advertising.amazon.com/campaign-manager",
    is_headless: int = 0,
    cookie_save: int = 0,
) -> dict[str, Any]:
    """在紫鸟浏览器中打开指定店铺，连接 Playwright 并自动导航到目标页面。

打开后全局会话保持浏览器连接，后续可用 rpa_click、rpa_fill_input 等原子工具操作。
导航后自动检测是否为登录页面，Agent 可根据 is_login_page 字段判断下一步。"""
    # 已有同一店铺的活跃会话 → 直接返回
    if _rpa_session.active and _rpa_session.page is not None:
        existing_oauth = _rpa_session.browser_oauth
        if existing_oauth and (existing_oauth == store_id or store_id.isdigit()):
            page = _rpa_session.page
            return {
                "status": "success",
                "message": f"店铺 {store_name or store_id} 已处于打开状态，无需重复打开。",
                "browser_oauth": existing_oauth,
                "page_summary": _build_page_summary(page),
            }

    # 换店：先关闭旧店铺
    old_oauth = _rpa_session.browser_oauth
    if old_oauth:
        try:
            close_data = {
                "action": "stopBrowser",
                "requestId": str(uuid.uuid4()),
                "duplicate": 0,
                "browserOauth": old_oauth,
            }
            close_data.update(_get_ziniao_user())
            _send_http(close_data)
        except Exception:
            pass

    _rpa_reset_session()

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
        "privacyMode": 0,
    }
    data.update(_get_ziniao_user())

    if store_id.isdigit():
        data["browserId"] = store_id
    else:
        data["browserOauth"] = store_id

    r = _send_http(data)
    if r is None:
        return {"status": "error", "message": "无法连接紫鸟客户端"}
    if str(r.get("statusCode")) != "0":
        return {
            "status": "error",
            "message": f"打开店铺失败: {json.dumps(r, ensure_ascii=False)}",
        }

    browser_oauth = r.get("browserId") or r.get("browserOauth", "")
    debugging_port = r.get("debuggingPort")
    ip_check_url = r.get("ipDetectionPage", "")

    if not debugging_port or not ip_check_url:
        return {
            "status": "error",
            "message": "店铺打开成功但缺少连接信息（debuggingPort/ipDetectionPage），请升级紫鸟浏览器",
        }

    # Playwright CDP 连接
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        return {"status": "error", "message": "未安装 playwright: pip install playwright"}

    steps: list[str] = []

    try:
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debugging_port}")
        context = browser.contexts[0]
        page = context.pages[0]
        steps.append("Playwright 已连接")
    except Exception as exc:
        _rpa_reset_session()
        return {"status": "error", "message": f"CDP 连接失败: {exc}"}

    # IP 检测
    try:
        page.goto(ip_check_url)
        btn = page.locator("//button[contains(@class, 'styles_btn--success')]")
        btn.wait_for(timeout=60000)
        steps.append("IP 检测通过")
    except PwTimeout:
        _rpa_reset_session()
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass
        return {"status": "error", "message": "IP 检测超时，代理可能异常"}

    # 自动导航到目标页面
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        steps.append(f"已导航到 {target_url}")
    except Exception:
        steps.append(f"导航到 {target_url} 超时（页面可能仍在加载）")

    time.sleep(3)

    # 保存全局会话
    _rpa_session.active = True
    _rpa_session.playwright = pw
    _rpa_session.browser = browser
    _rpa_session.context = context
    _rpa_session.page = page
    _rpa_session.browser_oauth = browser_oauth
    _rpa_session.store_name = store_name

    summary = _build_page_summary(page)
    is_login = _detect_login_page(summary)

    return {
        "status": "success",
        "browser_oauth": browser_oauth,
        "debugging_port": debugging_port,
        "target_url": target_url,
        "steps": steps,
        "is_login_page": is_login,
        "page_summary": summary,
        "message": (
            f"店铺 {store_name or store_id} 已打开，已导航到 {target_url}。"
            + (" ⚠ 当前显示登录页面，需先登录 Amazon 账号。" if is_login
               else " 页面已正常加载。")
        ),
    }


@tool(args_schema=CloseStoreArgs)
def rpa_close_store(browser_oauth: str = "") -> dict[str, Any]:
    """关闭紫鸟浏览器中已打开的店铺，同时断开 Playwright。"""
    oauth = browser_oauth or _rpa_session.browser_oauth
    _rpa_reset_session()
    if not oauth:
        return {"status": "error", "message": "未提供 browser_oauth 且无活跃会话"}

    data = {
        "action": "stopBrowser",
        "requestId": str(uuid.uuid4()),
        "duplicate": 0,
        "browserOauth": oauth,
    }
    data.update(_get_ziniao_user())
    r = _send_http(data)

    if r is None:
        return {"status": "success", "message": "Playwright 已断开（紫鸟无响应）"}
    if str(r.get("statusCode")) == "0":
        return {"status": "success", "message": f"已关闭店铺 {oauth}"}
    return {
        "status": "error",
        "message": f"关闭店铺失败: {json.dumps(r, ensure_ascii=False)}",
    }


def _is_allowed_store(name_or_id: str) -> bool:
    if name_or_id in ALLOWED_RPA_STORES:
        return True
    if name_or_id.isdigit():
        return name_or_id in STORES.values()
    return False


def resolve_store_id(name_or_id: str) -> str | None:
    """根据店铺名称查找 ID。只允许白名单内的店铺。"""
    if name_or_id.isdigit():
        for store_name, store_id in STORES.items():
            if store_id == name_or_id:
                return store_id
        return None

    if name_or_id not in ALLOWED_RPA_STORES:
        return None

    if name_or_id in STORES:
        return STORES[name_or_id]
    result = rpa_store_list.invoke({})
    if result.get("status") == "success":
        for s in result["stores"]:
            if s.get("browserName") == name_or_id:
                return s.get("browserOauth")
    return None


# ---------------------------------------------------------------------------
# 原子浏览器操作
# ---------------------------------------------------------------------------

def _detect_login_page(summary: dict[str, Any]) -> bool:
    title = (summary.get("title") or "").lower()
    url = (summary.get("url") or "").lower()
    text = (summary.get("body_text_preview") or "").lower()

    if "amazon" in url and ("signin" in url or "ap/signin" in url):
        return True
    if any(kw in title for kw in ["sign in", "sign-in", "login", "log in", "amazon sign-in"]):
        return True
    if any(kw in text for kw in ["ap_email", "ap_password", "keep me signed in",
                                   "email or mobile phone number"]):
        return True
    return False


def _build_page_summary(page) -> dict[str, Any]:
    try:
        title = page.title()
    except Exception:
        title = "(无法获取)"

    try:
        buttons = page.locator(
            "button:visible, a:visible, [role='button']:visible, "
            "input[type='submit']:visible, input[type='button']:visible"
        ).all_text_contents()
        seen = set()
        unique_buttons = []
        for b in buttons:
            b = b.strip()[:80]
            if b and b not in seen:
                seen.add(b)
                unique_buttons.append(b)
        buttons_text = unique_buttons[:30]
    except Exception:
        buttons_text = ["(提取失败)"]

    try:
        inputs = page.locator("input:visible, textarea:visible, select:visible").evaluate_all("""
            els => els.slice(0, 15).map(el => ({
                placeholder: el.placeholder || '',
                name: el.name || '',
                id: el.id || '',
                type: el.type || el.tagName.toLowerCase(),
                value: el.value || ''
            }))
        """)
    except Exception:
        inputs = []

    try:
        page_text = page.locator("body").inner_text()
        page_text = page_text[:2000]
    except Exception:
        page_text = "(无法提取)"

    return {
        "title": title,
        "url": page.url[:200],
        "buttons": buttons_text,
        "inputs": inputs,
        "body_text_preview": page_text,
    }


@tool
def rpa_page_summary() -> dict[str, Any]:
    """获取当前浏览器页面的文本摘要：标题、URL、可见按钮列表、输入框列表、页面正文预览。
在每次页面变化后（点击、导航、填充）应调用此工具了解当前状态，然后决定下一步操作。"""
    page = _rpa_get_page()
    if page is None:
        return {"status": "error", "message": "没有活跃的浏览器会话。请先调用 rpa_open_store 打开店铺。"}
    try:
        summary = _build_page_summary(page)
        return {"status": "success", **summary}
    except Exception as exc:
        _rpa_session.last_error = str(exc)
        return {"status": "error", "message": f"获取页面摘要失败: {exc}"}


@tool(args_schema=RpaClickArgs)
def rpa_click(text: str, index: int = 0) -> dict[str, Any]:
    """点击页面上包含指定文本的可见元素（按钮/链接）。点击后自动返回新页面摘要。"""
    page = _rpa_get_page()
    if page is None:
        return {"status": "error", "message": "没有活跃的浏览器会话。请先调用 rpa_open_store 打开店铺。"}

    if not text.strip():
        return {"status": "error", "message": "请提供要点击的元素文本"}

    try:
        locator = page.get_by_role("button", name=text)
        if locator.count() == 0:
            locator = page.get_by_role("link", name=text)
        if locator.count() == 0:
            locator = page.get_by_role("option", name=text)
        if locator.count() == 0:
            locator = page.locator(f"//*[contains(text(), '{text}')]")

        count = locator.count()
        if count == 0:
            return {
                "status": "error",
                "message": f"未找到包含文本 '{text}' 的可点击元素。建议调用 rpa_page_summary 查看可用按钮。",
            }

        target = locator.nth(min(index, count - 1))
        target.click(force=True, timeout=10000)

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            page.wait_for_timeout(1500)

        summary = _build_page_summary(page)
        return {
            "status": "success",
            "clicked": text,
            "matched_count": count,
            "page_summary": summary,
        }
    except Exception as exc:
        _rpa_session.last_error = str(exc)
        return {"status": "error", "message": f"点击失败: {exc}"}


@tool(args_schema=RpaFillInputArgs)
def rpa_fill_input(placeholder_or_name: str, value: str) -> dict[str, Any]:
    """在输入框中填入文本。通过 placeholder 文本或 name 属性定位输入框。"""
    page = _rpa_get_page()
    if page is None:
        return {"status": "error", "message": "没有活跃的浏览器会话。请先调用 rpa_open_store 打开店铺。"}

    try:
        locator = page.get_by_placeholder(placeholder_or_name)
        if locator.count() == 0:
            locator = page.locator(f"input[name='{placeholder_or_name}']")
        if locator.count() == 0:
            locator = page.locator(f"//input[@id='{placeholder_or_name}']")
        if locator.count() == 0:
            return {
                "status": "error",
                "message": f"未找到 placeholder='{placeholder_or_name}' 的输入框。"
                           f"建议调用 rpa_page_summary 查看可用输入框。",
            }

        locator.first.fill(value)
        return {"status": "success", "filled": placeholder_or_name, "value": value}
    except Exception as exc:
        _rpa_session.last_error = str(exc)
        return {"status": "error", "message": f"填充失败: {exc}"}


@tool(args_schema=RpaNavigateArgs)
def rpa_navigate(url: str) -> dict[str, Any]:
    """导航到指定 URL。导航后自动返回新页面摘要。"""
    page = _rpa_get_page()
    if page is None:
        return {"status": "error", "message": "没有活跃的浏览器会话。请先调用 rpa_open_store 打开店铺。"}

    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state(state="load")
        summary = _build_page_summary(page)
        return {
            "status": "success",
            "navigated_to": url,
            "page_summary": summary,
        }
    except Exception as exc:
        _rpa_session.last_error = str(exc)
        return {"status": "error", "message": f"导航失败: {exc}"}


@tool(args_schema=RpaExtractTablesArgs)
def rpa_extract_tables(max_rows: int = 50) -> dict[str, Any]:
    """提取当前页面上所有 HTML 表格的结构化数据（表头 + 数据行）。"""
    page = _rpa_get_page()
    if page is None:
        return {"status": "error", "message": "没有活跃的浏览器会话。请先调用 rpa_open_store 打开店铺。"}

    try:
        tables = page.evaluate(f"""
            () => {{
                return Array.from(document.querySelectorAll('table')).slice(0, 5).map(table => {{
                    const headers = Array.from(table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td'))
                        .map(c => c.innerText.trim());
                    const rows = Array.from(table.querySelectorAll('tbody tr, tr'))
                        .slice(0, {max_rows})
                        .map(row =>
                            Array.from(row.querySelectorAll('td, th'))
                                .map(c => c.innerText.trim())
                        )
                        .filter(row => row.length > 0);
                    return {{ headers, rows }};
                }});
            }}
        """)
        return {"status": "success", "tables": tables, "table_count": len(tables)}
    except Exception as exc:
        return {"status": "error", "message": f"提取表格失败: {exc}"}


@tool(args_schema=RpaWaitArgs)
def rpa_wait(seconds: int = 2) -> dict[str, Any]:
    """等待指定秒数（用于等待页面异步加载）。"""
    page = _rpa_get_page()
    if page is None:
        return {"status": "error", "message": "没有活跃的浏览器会话。请先调用 rpa_open_store 打开店铺。"}

    try:
        wait_time = min(max(1, seconds), 30)
        page.wait_for_timeout(wait_time * 1000)
        return {"status": "success", "waited": wait_time}
    except Exception as exc:
        return {"status": "error", "message": f"等待失败: {exc}"}


@tool(args_schema=RpaScrollArgs)
def rpa_scroll(direction: str = "down", amount: int = 500) -> dict[str, Any]:
    """滚动浏览器页面。"""
    page = _rpa_get_page()
    if page is None:
        return {"status": "error", "message": "没有活跃的浏览器会话。请先调用 rpa_open_store 打开店铺。"}

    pixels = amount if direction == "down" else -amount
    page.evaluate(f"window.scrollBy(0, {pixels})")
    return {"status": "success", "scrolled": f"{direction} {abs(pixels)}px"}


@tool(args_schema=RpaQueryCampaignSpendArgs)
def rpa_query_campaign_spend(site_keywords_json: str = "") -> dict[str, Any]:
    """查询 Amazon 广告花费。自包含全流程：打开店铺→IP检测→导航→日期筛选→国家筛选→逐关键词搜索→提取花费→关闭店铺。

无需先调用 rpa_open_store。操作人如果没有特别要求就只填操作人。"""
    try:
        original_result = json.loads(site_keywords_json) if site_keywords_json else None

        if not original_result:
            file_path = os.getenv("CAMPAIGN_SPEND_EXCEL_PATH")
            if not file_path:
                raise RuntimeError("CAMPAIGN_SPEND_EXCEL_PATH 未设置，无法读取广告花费数据表")
            original_result = read_excel_to_nested_dict(file_path)

        if not isinstance(original_result, dict):
            return {
                "status": "error",
                "message": (
                    "参数格式错误：site_keywords_json 必须是对象而非数组或字符串。"
                    "正确格式: {\"操作人\": {\"站点名\": [\"关键词1\", \"关键词2\"]}}。"
                ),
            }
        for operator, sites_dict in original_result.items():
            if not isinstance(sites_dict, dict):
                return {
                    "status": "error",
                    "message": (
                        f"参数格式错误：操作人 \"{operator}\" 对应的值必须是对象（站点→关键词列表），"
                        f"当前类型为 {type(sites_dict).__name__}。"
                    ),
                }
            for site, keywords in sites_dict.items():
                if not isinstance(keywords, list):
                    return {
                        "status": "error",
                        "message": (
                            f"参数格式错误：操作人 \"{operator}\" 站点 \"{site}\" 的关键词必须是数组，"
                            f"当前类型为 {type(keywords).__name__}。"
                        ),
                    }

        kill_process(version="v6")
        start_browser()
        update_core()

        include_null_result = excute_result(original_result)
        result = excute_result_with_retry(include_null_result, max_retry_times=3)
        get_exit()
        return {"status": "success", "message": result}
    except Exception as exc:
        traceback.print_exc()
        return {"status": "error", "message": f"查询关键词花费失败: {exc}"}


# ---------------------------------------------------------------------------
# 工具收集
# ---------------------------------------------------------------------------

def get_rpa_tools() -> list:
    """返回所有 RPA @tool 函数列表，供 get_skill_tools() 使用。"""
    return [
        rpa_kill_process,
        rpa_start_browser,
        rpa_update_core,
        rpa_exit_client,
        rpa_store_list,
        rpa_open_store,
        rpa_close_store,
        rpa_page_summary,
        rpa_click,
        rpa_fill_input,
        rpa_navigate,
        rpa_extract_tables,
        rpa_wait,
        rpa_scroll,
        rpa_query_campaign_spend,
    ]
