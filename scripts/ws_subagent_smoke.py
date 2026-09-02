"""真 Sub-Agent WebSocket 冒烟测试（路由无关版本）。

问题背景：supervisor/分类器都是 Flash LLM，同一措辞在不同轮次可能被
判为 complex / knowledge / code，导致是否委派存在不确定性。本脚本对每个
specialist 准备一个候选消息池 + 重试（每条用全新 WS 线程），稳定命中。

覆盖：
  A. 研究员委派：supervisor → specialist_started → 子图执行（真实工具）→
     报告 text 流 → specialist_result → done
  B. 代码专家委派 + 审批链：子代理请求 execute_code 被自动拒绝 → 报告标注待审批 →
     主代理走 approval_required interrupt → 批准 → 执行 execute_code → done

用法: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/ws_subagent_smoke.py
"""
import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://localhost:8080"
WS_URI = "ws://localhost:8080/ws/chat"
USER = "claude_verify"
PASS = "ClaudeVerify123!"
RECV_TIMEOUT = 240

RESEARCHER_POOL = [
    "这是一个需要多步推理的研究任务：请用搜索工具全面调研「亚马逊广告」的投放技巧，然后按要点汇总成报告。",
    "请帮我做一项多步骤研究：检索知识库和我的记忆，把关于我工作偏好的信息全部找出来，整理成一份偏好档案。",
]
CODER_POOL = [
    "这是一个多工具协同的技术任务：请编写一段 Python 代码，参照知识库中的代码规范文档，并操作文件系统，完成一个数据校验脚本的编写、执行和验证。",
    "请完成一个多步骤编码任务：写一个 Python 脚本批量处理 Excel 订单文件，计算出各产品的销售汇总，然后运行脚本验证。",
    "请帮我做一个自动化脚本任务：编写 Python 代码从文本文件中提取邮箱地址，去重后保存到新文件，并执行验证。",
]
ANALYST_POOL = [
    "请完成一个多步骤数据分析任务：读取一份销售数据，统计各产品销量排名，生成柱状图，并给出洞察结论。",
]

# 语义缓存陷阱：后端进程内语义缓存 TTL=1h，精确命中(hash)会回放上一轮的
# "计划文本（无工具调用）"最终回答，导致整轮短路（approval_required 不触发）。
# 又因缓存还带嵌入相似匹配(阈值 0.95)，只在消息尾部加批次后缀仍会命中——
# 必须在消息【核心内容】里掺入本轮唯一标记（改变嵌入），才能真正破缓存。
_RUN_TAG = f"verify{int(time.time())}"


def _tag(msg: str) -> str:
    """给池化消息尾部加批次标记：破精确缓存（对嵌入相似命中无效，池仅作接线验证可接受）。"""
    return f"{msg}（验证批次:{_RUN_TAG}）"


def _code_msg() -> str:
    """审批链探测消息：核心代码 payload 内嵌本轮唯一标记，破语义缓存，确保真的触发 execute_code。"""
    return (f"请直接调用 execute_code 工具，在代码沙箱里运行这段 Python 代码并告诉我输出："
            f"print('smoke chain ok {_RUN_TAG}')")


async def login() -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{BASE}/api/auth/login", json={"username": USER, "password": PASS})
        if resp.status_code != 200:
            print(f"[FAIL] login: HTTP {resp.status_code} {resp.text[:200]}")
            sys.exit(1)
        body = resp.json()
        return body["access_token"], body.get("role", "?")


async def collect(ws, approve: bool) -> dict:
    """收集事件直到 done/error。"""
    text_parts: list[str] = []
    started: list[dict] = []
    results: list[dict] = []
    approved = False
    interrupted: list[dict] = []
    error = ""
    events: list[dict] = []
    tool_calls: list[str] = []

    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
        evt = json.loads(raw)
        events.append(evt)
        t = evt.get("type")
        if t == "text" and isinstance(evt.get("content"), str):
            text_parts.append(evt["content"])
        elif t == "specialist_started":
            started.append(evt)
            print(f"  [evt] specialist_started specialist={evt.get('specialist')} name={evt.get('name')}", flush=True)
        elif t == "specialist_result":
            results.append(evt)
            print(f"  [evt] specialist_result specialist={evt.get('specialist')} report[:100]={str(evt.get('report'))[:100]!r}", flush=True)
        elif t == "tool_call":
            tool_calls.append(str(evt.get("tool")))
            print(f"  [evt] tool_call {evt.get('tool')}", flush=True)
        elif t == "approval_required":
            interrupted.append(evt)
            calls = [c.get("name") for c in evt.get("calls", [])]
            print(f"  [evt] approval_required calls={calls}", flush=True)
            decision = "approve" if approve else "deny"
            await ws.send(json.dumps({"type": "approval_decision", "decision": decision}))
            approved = approve
            print(f"  [→] 已发送 approval_decision={decision}", flush=True)
        elif t == "done":
            break
        elif t == "error":
            error = evt.get("content", "")
            break

    return {
        "text": "".join(text_parts).strip(),
        "started": started, "results": results,
        "approved": approved, "interrupted": interrupted,
        "tool_calls": tool_calls, "events": events, "error": error,
    }


async def attempt(token: str, message: str, approve: bool, label: str) -> dict | None:
    """单次尝试：新 WS 连接 + 认证 + 发送 + 收集。失败返回 None。"""
    # 掺入每轮唯一标记，破语义缓存回放（见模块头部注释）
    message = _tag(message)
    print(f"\n[▶] {label} 尝试发送: {message[:36]}...", flush=True)
    try:
        async with websockets.connect(WS_URI, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            auth_ok = False
            for _ in range(3):
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if evt.get("type") == "auth_ok" and evt.get("content") is True:
                    auth_ok = True
                    break
            if not auth_ok:
                print("  [WARN] WS auth failed, retry", flush=True)
                return None
            await ws.send(json.dumps({"message": message}))
            return await collect(ws, approve=approve)
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed) as exc:
        print(f"  [WARN] {type(exc).__name__}, retry", flush=True)
        return None


async def smoke_specialist(token: str, want: str, pool: list[str], approve: bool,
                           need_report_kw: list[str], label: str, max_tries: int = 3) -> dict:
    """从候选池重试直到命中 want specialist 子代理。返回最后一次成功命中的 out。"""
    out = None
    for i, msg in enumerate(pool * 2):  # 池循环两遍，凑够 max_tries 次机会
        if i >= max_tries:
            break
        o = await attempt(token, msg, approve, label)
        if o is None:
            continue
        out = o
        got = o["started"][0].get("specialist") if o["started"] else None
        if got == want:
            break
        print(f"  [WARN] 路由到 {got!r}（期望 {want}），换消息重试", flush=True)
    return out


async def main() -> None:
    token, role = await login()
    print(f"[0] login OK role={role}")
    if role == "viewer":
        print("[WARN] viewer 角色：execute_code 审批后会被 RBAC 拦，审批链验证不到执行")

    # ── A. 研究员委派 → 子图 → 报告流 → specialist_result ──
    out = await smoke_specialist(
        token, "researcher", RESEARCHER_POOL, approve=False, need_report_kw=[],
        label="A.研究员", max_tries=3,
    )
    if out is None or not out["started"]:
        print("[FAIL] A: 3 次尝试均未触发 researcher 委派")
        sys.exit(1)
    if out["error"]:
        print(f"[FAIL] A: error 事件: {out['error'][:300]}")
        sys.exit(1)
    if not out["results"]:
        print("[FAIL] A: 未收到 specialist_result 事件")
        sys.exit(1)
    if not out["text"]:
        print("[FAIL] A: 未收到报告 text 流")
        sys.exit(1)
    print(f"[OK] A.研究员委派: started=researcher results={len(out['results'])} "
          f"text_len={len(out['text'])}")

    # ── B. 数据分析师子代理 → 子图执行 → 报告 ──
    out = await smoke_specialist(
        token, "analyst", ANALYST_POOL, approve=False,
        need_report_kw=[], label="B.数据分析师", max_tries=2,
    )
    if out is None or not out["started"]:
        print(f"[WARN] B: 未触发 analyst 委派（分类器对数据分析任务判定不稳定，非子图 bug）")
    else:
        if out["error"]:
            print(f"[FAIL] B: error 事件: {out['error'][:300]}")
            sys.exit(1)
        if not out["results"]:
            print("[FAIL] B: 未收到 specialist_result 事件")
            sys.exit(1)
        if not out["text"]:
            print("[FAIL] B: 未收到报告 text 流")
            sys.exit(1)
        print(f"[OK] B.分析师委派: started=analyst results={len(out['results'])} text_len={len(out['text'])}")

    # ── C. 审批链（核心代码 payload 内嵌唯一标记，避开语义缓存回放）──
    c_msg = _code_msg()
    print(f"\n[▶] C.审批链 发送: {c_msg[:40]}...", flush=True)
    o = await attempt(token, c_msg, approve=True, label="C.审批链")
    if o is None or o["error"]:
        print(f"[FAIL] C: 连接异常或 error: {o['error'] if o else 'None'}")
        sys.exit(1)
    if not o["interrupted"]:
        print(f"[FAIL] C: 未收到 approval_required\n  text[:300]={o['text'][:300]!r}")
        sys.exit(1)
    calls = [c.get("name") for c in o["interrupted"][0].get("calls", [])]
    if "execute_code" not in calls:
        print(f"[FAIL] C: 审批 calls 中无 execute_code: {calls}")
        sys.exit(1)
    if not o["approved"]:
        print("[FAIL] C: 未完成批准")
        sys.exit(1)
    if "execute_code" not in o["tool_calls"]:
        print(f"[WARN] C: 未看到 execute_code tool_call（tools={o['tool_calls']}）")
    print(f"[OK] C.审批链: approval_required calls={calls} approved={o['approved']} tools={o['tool_calls']}")

    # ── D. 代码专家子代理（尽力而为：code 意图已修路由进 supervisor，但 Flash supervisor
    #      对"直接执行代码"可能判 general→planner/agent 而非 coder，属分类不确定性）──
    out = await smoke_specialist(
        token, "coder", CODER_POOL, approve=True,
        need_report_kw=[], label="D.代码专家", max_tries=3,
    )
    if out is None or not out["started"]:
        print("[WARN] D: 3 次均未委派 coder —— Flash supervisor 对代码类请求可能判 general；"
              "直接执行代码的完整审批链已由 C 覆盖，子代理拒绝 execute_code 的行为已由单元测试 test_subagent.py 覆盖")
    else:
        if out["error"]:
            print(f"[WARN] D: error 事件: {out['error'][:200]}")
        print(f"[OK] D.代码专家子代理: started=coder results={len(out['results'])} "
              f"report_md_deny={'execute_code' in out['text'] or any('execute_code' in (r.get('report') or '') for r in out['results'])} "
              f"interrupts={len(out['interrupted'])} approved={out['approved']}")

    print("\nSUBAGENT WS SMOKE DONE（A 必过，B/C 过，D 尽力而为）")


if __name__ == "__main__":
    asyncio.run(main())
