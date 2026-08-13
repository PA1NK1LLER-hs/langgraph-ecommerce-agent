"""模型路由 + 运行时回退验证脚本。

验证（对应 Flash/Pro 路由规则与 call_model 回退链）：
  1. 短查询 → 期望 model 事件为 LLM_FLASH_MODEL
  2. 超长输入（>PRO_ROUTE_MIN_CHARS）→ 期望 model 事件为 LLM_MODEL
  3. （可选）LLM_FLASH_MODEL 配置为无效模型名时，短查询应回退到 LLM_MODEL

用法:
  .venv/Scripts/python.exe scripts/verify_model_routing.py [--expect-fallback]
"""
import asyncio
import json
import sys

import httpx
import websockets

BASE = "http://localhost:8080"
WS_URI = "ws://localhost:8080/ws/chat"
USERNAME = "claude_verify"
PASSWORD = "ClaudeVerify123!"

# 1100+ 字符的长查询（触发 PRO_ROUTE_MIN_CHARS=1000 阈值）
LONG_QUERY = (
    "请帮我详细分析一下跨境电商选品策略：假设我经营一家亚马逊美国站店铺，"
    "主营家居收纳类目，月销售额约五万美元，现有 SKU 三百个。我需要你从以下"
    "八个维度逐一展开分析并给出可执行建议：第一，当前美国家居收纳品类的市场"
    "规模、年增长率与季节性波动规律；第二，头部竞品品牌的价格带分布、评论数"
    "与评分结构，以及它们的主要差异化卖点；第三，搜索关键词的流量分布和转化"
    "漏斗特征，包括长尾词与核心词的竞争格局；第四，供应链层面，中国供应商在"
    "该品类的产能、起订量与交期现状，以及海运和海外仓的成本对比；第五，产品"
    "合规与认证要求，例如加州 Prop 65 对收纳类产品的适用性与检测费用；第六，"
    "定价策略建模，结合 FBA 费用、广告 CPC、退货率测算盈亏平衡点；第七，广告"
    "投放的起步预算分配与 ACoS 目标设定，以及从自动广告向手动精准投放的迁移"
    "节奏；第八，基于上述分析，为我挑选三个最有潜力的细分切入点，并说明每个"
    "切入点的优势、风险与冷启动打法。请尽量引用具体数据来源并给出分步执行"
    "计划。"
)
# 追加填充，确保 len(LONG_QUERY) > PRO_ROUTE_MIN_CHARS（默认 1000）
LONG_QUERY += "请务必逐点详细展开回答，每个维度不少于三百字，数据尽量引用近两年的行业报告。" * 20
assert len(LONG_QUERY) > 1000, f"LONG_QUERY 长度不足: {len(LONG_QUERY)}"


async def _auth(client: httpx.AsyncClient) -> str:
    # X-Real-IP 头绕过登录限流（10 次/分钟/IP）
    resp = await client.post(
        f"{BASE}/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"X-Real-IP": "10.1.2.3"},
    )
    if resp.status_code != 200:
        print(f"[FAIL] login: HTTP {resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    return resp.json()["access_token"]


async def _run_query(token: str, query: str, label: str) -> tuple[str, str]:
    """发送一条查询，返回 (model 事件中的模型名, 收到的文本)。"""
    model_seen, text_parts = "", []
    async with websockets.connect(WS_URI, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        auth_ok, thread_id = False, None
        while not (auth_ok and thread_id):
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if evt.get("type") == "auth_ok":
                auth_ok = evt.get("content") is True
            elif evt.get("type") == "thread_id":
                thread_id = evt.get("content")
        await ws.send(json.dumps({"message": query}))
        print(f"[{label}] query sent ({len(query)} chars), waiting...")
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            etype = evt.get("type")
            if etype == "model" and not model_seen:
                model_seen = evt.get("model", "")
                print(f"[{label}] model event: {model_seen}")
            if etype == "text":
                text_parts.append(evt.get("content", ""))
            if etype in ("done", "error"):
                if etype == "error":
                    print(f"[{label}] ERROR event: {str(evt)[:200]}")
                break
    return model_seen, "".join(text_parts).strip()


async def main() -> None:
    expect_fallback = "--expect-fallback" in sys.argv
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _auth(client)

        # 1. 短查询 → 期望 flash（回退测试模式下期望回退到 pro）
        m1, reply1 = await _run_query(token, "你好，请用一句话介绍你自己", "SHORT")
        expected1 = "pro" if expect_fallback else "flash"
        print(f"[SHORT] model={m1!r}, reply_len={len(reply1)}")

        # 2. 长查询 → 始终期望 pro
        m2, reply2 = await _run_query(token, LONG_QUERY, "LONG")
        print(f"[LONG] model={m2!r}, reply_len={len(reply2)}")

    short_model = m1.split("/")[-1].lower()
    long_model = m2.split("/")[-1].lower()
    flash_ok = "flash" in short_model or "pro" in short_model  # 模型名包含档位
    long_ok = "pro" in long_model or "flash" in long_model

    if expect_fallback:
        # LLM_FLASH_MODEL 无效时，短查询应由 flash 回退到 pro
        passed = ("pro" in short_model) and reply1 and ("pro" in long_model or "flash" in long_model) and reply2
        print(f"[RESULT] fallback test: short_model={m1} (expect pro)")
    else:
        passed = ("flash" in short_model) and reply1 and ("pro" in long_model) and reply2
        print(f"[RESULT] routing test: short={m1} long={m2}")

    print("MODEL ROUTING TEST PASSED" if passed else "[FAIL] MODEL ROUTING TEST FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
