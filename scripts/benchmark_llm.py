"""LLM 速度对比 — Token Plan vs MiMo 按量 vs DeepSeek。

运行方式（通过环境变量设置密钥，不硬编码）:
  set BENCHMARK_TOKEN_PLAN_KEY=tp-xxx
  set BENCHMARK_MIMO_KEY=sk-xxx
  set BENCHMARK_DEEPSEEK_KEY=sk-xxx
  python scripts/benchmark_llm.py
"""

import os
import time
import sys
from pathlib import Path
from openai import OpenAI

_TOKEN_PLAN_KEY = os.getenv("BENCHMARK_TOKEN_PLAN_KEY", "")
_MIMO_KEY = os.getenv("BENCHMARK_MIMO_KEY", "")
_DEEPSEEK_KEY = os.getenv("BENCHMARK_DEEPSEEK_KEY", "")

PROVIDERS = {
    "Token Plan (Flash)": {
        "api_key": _TOKEN_PLAN_KEY,
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "model": "mimo-v2.5",
        "uses_custom_auth": True,
    },
    "Token Plan (Pro)": {
        "api_key": _TOKEN_PLAN_KEY,
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "model": "mimo-v2.5-pro",
        "uses_custom_auth": True,
    },
    "MiMo 按量 (Flash)": {
        "api_key": _MIMO_KEY,
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2-flash",
        "uses_custom_auth": False,
    },
    "MiMo 按量 (Pro)": {
        "api_key": _MIMO_KEY,
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-pro",
        "uses_custom_auth": False,
    },
    "DeepSeek (Flash)": {
        "api_key": _DEEPSEEK_KEY,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "uses_custom_auth": False,
    },
    "DeepSeek (Pro)": {
        "api_key": _DEEPSEEK_KEY,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro",
        "uses_custom_auth": False,
    },
}

PROMPT = "请用一句话介绍你自己，不超过20个字。"
ROUNDS = 5
TIMEOUT = 60  # 单次请求超时秒数


def create_client(cfg: dict) -> OpenAI:
    if cfg["uses_custom_auth"]:
        return OpenAI(
            api_key="not-used",
            base_url=cfg["base_url"],
            default_headers={"api-key": cfg["api_key"]},
            timeout=TIMEOUT,
        )
    else:
        return OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            timeout=TIMEOUT,
        )


def test_one(name: str, cfg: dict) -> dict | None:
    client = create_client(cfg)
    model = cfg["model"]
    ttfts: list[float] = []      # time-to-first-token
    totals: list[float] = []     # total time
    token_counts: list[int] = []

    for i in range(ROUNDS):
        try:
            t0 = time.perf_counter()
            stream = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT}],
                max_tokens=100,
                stream=True,
                stream_options={"include_usage": True},
            )
            first_token = None
            content = ""
            for chunk in stream:
                if first_token is None:
                    first_token = time.perf_counter()
                if chunk.choices and chunk.choices[0].delta.content:
                    content += chunk.choices[0].delta.content
            t_end = time.perf_counter()

            ttft = (first_token - t0) if first_token else None
            total = t_end - t0
            ttfts.append(ttft)
            totals.append(total)
            token_counts.append(len(content))
            safe_content = content.encode("utf-8", errors="replace").decode("utf-8", errors="replace")[:50]
            print(f"  [{i+1}/{ROUNDS}] TTFT={ttft:.2f}s  Total={total:.2f}s  "
                  f"Chars={len(content)}  Response={safe_content}")

        except Exception as exc:
            print(f"  [{i+1}/{ROUNDS}] FAIL: {exc}")
            continue

    if not totals:
        return None

    return {
        "name": name,
        "model": model,
        "ttft_avg": sum(ttfts) / len(ttfts) if ttfts else None,
        "ttft_min": min(ttfts) if ttfts else None,
        "ttft_max": max(ttfts) if ttfts else None,
        "total_avg": sum(totals) / len(totals),
        "total_min": min(totals),
        "total_max": max(totals),
        "success": f"{len(totals)}/{ROUNDS}",
    }


def main():
    print("=" * 70)
    print("LLM 速度对比基准测试")
    print(f"Prompt: {PROMPT}")
    print(f"每厂商 {ROUNDS} 轮, timeout={TIMEOUT}s")
    print("=" * 70)

    # 先测 Flash 模型
    flash_names = [n for n in PROVIDERS if "Flash" in n]
    pro_names = [n for n in PROVIDERS if "Pro" in n]

    results = []

    print("\n── Flash 模型对比 ──")
    for name in flash_names:
        print(f"\n>>> {name} ({PROVIDERS[name]['model']})")
        r = test_one(name, PROVIDERS[name])
        if r:
            results.append(r)
        else:
            print(f"  SKIP: 全部请求失败")

    print("\n── Pro 模型对比 ──")
    for name in pro_names:
        print(f"\n>>> {name} ({PROVIDERS[name]['model']})")
        r = test_one(name, PROVIDERS[name])
        if r:
            results.append(r)
        else:
            print(f"  SKIP: 全部请求失败")

    # 汇总
    print("\n" + "=" * 70)
    print("结果汇总")
    print("=" * 70)

    # Flash 汇总
    print("\n─ Flash 模型 ─")
    print(f"{'Provider':<24} {'Model':<20} {'TTFT avg':>8} {'TTFT min':>8} {'Total avg':>9} {'Total min':>9} {'OK':>5}")
    print("-" * 90)
    for r in results:
        if "Flash" in r["name"]:
            ttft = f"{r['ttft_avg']:.2f}s" if r['ttft_avg'] else "N/A"
            ttft_min = f"{r['ttft_min']:.2f}s" if r['ttft_min'] else "N/A"
            print(f"{r['name']:<24} {r['model']:<20} {ttft:>8} {ttft_min:>8} "
                  f"{r['total_avg']:.2f}s {'':>3} {r['total_min']:.2f}s {'':>3} {r['success']:>5}")

    # Pro 汇总
    print("\n─ Pro 模型 ─")
    print(f"{'Provider':<24} {'Model':<20} {'TTFT avg':>8} {'TTFT min':>8} {'Total avg':>9} {'Total min':>9} {'OK':>5}")
    print("-" * 90)
    for r in results:
        if "Pro" in r["name"]:
            ttft = f"{r['ttft_avg']:.2f}s" if r['ttft_avg'] else "N/A"
            ttft_min = f"{r['ttft_min']:.2f}s" if r['ttft_min'] else "N/A"
            print(f"{r['name']:<24} {r['model']:<20} {ttft:>8} {ttft_min:>8} "
                  f"{r['total_avg']:.2f}s {'':>3} {r['total_min']:.2f}s {'':>3} {r['success']:>5}")

    # 推荐
    print("\n─ 结论 ─")
    flash_results = [r for r in results if "Flash" in r and r["total_avg"]]
    if flash_results:
        flash_results.sort(key=lambda r: r["total_avg"])
        best = flash_results[0]
        print(f"最快 Flash: {best['name']} — 平均 {best['total_avg']:.2f}s, TTFT {best['ttft_avg']:.2f}s")

    pro_results = [r for r in results if "Pro" in r and r["total_avg"]]
    if pro_results:
        pro_results.sort(key=lambda r: r["total_avg"])
        best = pro_results[0]
        print(f"最快 Pro:   {best['name']} — 平均 {best['total_avg']:.2f}s, TTFT {best['ttft_avg']:.2f}s")


if __name__ == "__main__":
    main()
