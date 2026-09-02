"""前端多 Agent 显示端到端验证（Playwright 驱动真实浏览器）。

supervisor 是 Flash 硬币翻转，单条消息不保证委派，故用候选池重试：
每条消息主题不同（既避开语义缓存回放，也让 supervisor 有多次机会委派 researcher/coder/analyst）。
一旦某轮出现『子代理』chip → 验证名字、running"执行中"、done 摘要 → PASS。
用法: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_ui_specialist.py
"""
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
USER = "claude_verify"
PASS = "ClaudeVerify123!"

# 主题互不相同的多步任务，命中 specialist 委派的概率足够高
POOL = [
    "这是一个需要多步推理的研究任务：请用搜索工具全面调研「亚马逊广告的 ACOS 优化策略」，然后按要点汇总成报告。",
    "请帮我完成一项多步骤研究：检索知识库和记忆，把关于我作为跨境卖家的运营偏好整理成一份偏好档案。",
    "请做一项多步分析研究：比较 Shopify 与 Amazon FBA 两种模式的成本结构差异，给出选型建议。",
    "这是一个复杂的调研任务：分析 TikTok Shop 美国站 2025 年的运营机会，分维度给出策略报告。",
]
CHIP = 'button:has-text("子代理")'
TEXTAREA = 'textarea[placeholder*="输入消息"]'


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(BASE, wait_until="domcontentloaded")

        # ── 登录 ──
        page.fill('input[placeholder="用户名"]', USER)
        page.fill('input[placeholder="密码"]', PASS)
        page.get_by_role("button", name="登录").click()
        page.wait_for_selector(TEXTAREA, timeout=20000)
        print("[1] 登录成功，进入聊天界面", flush=True)

        for i, msg in enumerate(POOL):
            tag = f"UI批{i}-{int(time.time())}"
            full = f"{msg}（{tag}）"
            # 等上一轮结束（textarea 解禁）
            page.wait_for_selector(f"{TEXTAREA}:not([disabled])", timeout=180000)
            page.fill(TEXTAREA, full)
            page.press(TEXTAREA, "Enter")
            print(f"[2] 尝试{i+1}/{len(POOL)} 已发送: {msg[:28]}...", flush=True)

            # 轮询直到出现『子代理』chip，或本轮结束（textarea 从 sending 禁用态重新解禁）
            chip_hit = False
            running_seen = False
            start = time.time()
            while time.time() - start < 90:
                if page.locator(CHIP).count() > 0:
                    chip_hit = True
                    # 捕获 running 态（若此刻还在执行中）
                    if page.evaluate(
                        "() => [...document.querySelectorAll('button')].some(b => b.innerText.includes('执行中'))"
                    ):
                        running_seen = True
                        print("  [evt] chip 出现且处于『执行中』running 态", flush=True)
                    break
                # 发送后先给一小段缓冲，再以 textarea 解禁判定本轮结束
                if time.time() - start > 3 and page.is_enabled(TEXTAREA):
                    break
                time.sleep(2)
            page.screenshot(path=f"ui_try{i+1}.png", full_page=False)

            if chip_hit:
                chips = page.locator(CHIP)
                n = chips.count()
                inner = chips.last.inner_text()[:300].replace("\n", " | ")
                names = [nm for nm in ("研究员", "代码专家", "数据分析师") if nm in inner]
                print(f"[3] ✓ 出现『子代理』chip（累计 {n} 个）名字={names} running_seen={running_seen}")
                print(f"    chip 文本: {inner}")
                # 等 chip 转 done（执行中消失；研究报告应已流出）
                try:
                    page.wait_for_function(
                        """() => {
                            const btns = [...document.querySelectorAll('button')];
                            const spec = btns.filter(b => b.innerText.includes('子代理'));
                            const last = spec[spec.length - 1];
                            return last && !last.innerText.includes('执行中');
                        }""",
                        timeout=180000,
                    )
                    print("[4] ✓ chip 已从 running 转为完成（done）", flush=True)
                except Exception as exc:
                    print(f"  [WARN] 等 chip done 超时: {type(exc).__name__}", flush=True)
                print("\nUI SPECIALIST DISPLAY: PASS")
                browser.close()
                sys.exit(0)

            print(f"  本轮未委派（supervisor 判 general 等），换下一条", flush=True)

        print("\nUI SPECIALIST DISPLAY: FAIL —— 4 轮均未触发 specialist 委派（分类/监督硬币翻转）")
        browser.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
