# -*- coding: utf-8 -*-
"""冒烟：RPA 意图懒连接 — mcp_rpa_* 硬隐藏，submit_rpa_* 对 LLM 可见。

验证"RPA 永远独立进程 + 提交侧封装"在后端真实生效：
  1. GET /api/tools  启动状态：3 个 submit_rpa_*，无 mcp_rpa_* / 进程内 rpa_*；
  2. WS 聊天发送 rpa 意图消息 → 意图分类节点 ensure_mcp_for_intent("rpa")
     懒连接独立 RPA MCP（stdio 子进程），但 register=False → 不注册给 LLM；
  3. 回合结束后再次 GET /api/tools → mcp_rpa_* 仍被硬隐藏，submit_rpa_* 仍在
     （LLM 只能看到提交侧封装，杜绝直接调用阻塞回合）。

安全性：RPA 工具属 HIGH_RISK（审批 interrupt），本脚本若收到审批事件
一律 deny；WS 关闭也会默认 deny，绝不触发真实 RPA 执行。

运行：.venv/Scripts/python.exe scripts/test_rpa_lazy_connect_api.py
"""
import asyncio
import json
import sys
import time

import httpx
import websockets

import os

BASE = os.getenv("RPA_SMOKE_BASE", "http://127.0.0.1:8080")
WS_URL = os.getenv("RPA_SMOKE_WS", "ws://127.0.0.1:8080/ws/chat")
USER = "claude_verify"
PWD = "ClaudeVerify123!"


def _fake_ip() -> str:
    import random
    return ".".join(str(random.randint(1, 254)) for _ in range(4))


def _rpa_tools(d: dict) -> tuple[list[str], list[str], list[str]]:
    names = [t["name"] for t in d.get("tools", [])]
    submit = sorted(n for n in names if n.startswith("submit_rpa_"))
    mcp = sorted(n for n in names if n.startswith("mcp_rpa_"))
    inproc = sorted(n for n in names if n.startswith("rpa_"))
    return submit, mcp, inproc


async def main() -> int:
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name} {detail}")
        if not ok:
            failed += 1

    async with httpx.AsyncClient(timeout=15, headers={"X-Real-IP": _fake_ip()}) as client:
        # 1. 启动状态
        d0 = (await client.get(f"{BASE}/api/tools")).json()
        s0, m0, i0 = _rpa_tools(d0)
        check("启动时 3 个 submit_rpa_* 可见", len(s0) == 3, f"submit={s0}")
        check("启动时无 mcp_rpa_*", not m0, f"tools={d0['count']}")
        check("启动时无进程内 rpa_*", not i0, str(i0))
        print(f"   [pre] count={d0['count']} submit_rpa={len(s0)} mcp_rpa={m0} rpa_*={i0}")

        # 登录
        r = await client.post(
            f"{BASE}/api/auth/login",
            json={"username": USER, "password": PWD},
        )
        check("claude_verify 登录", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            return 1
        token = r.json()["access_token"]

        # 2. 触发 rpa 意图（懒连接独立 RPA MCP，但不注册给 LLM）
        async with websockets.connect(WS_URL, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            auth_ok, thread_id = False, None
            for _ in range(6):
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if evt.get("type") == "auth_ok":
                    auth_ok = evt.get("content") is True
                elif evt.get("type") == "thread_id":
                    thread_id = evt.get("content")
                if auth_ok and thread_id:
                    break
            check("WS auth_ok + thread_id", auth_ok and bool(thread_id), f"tid={thread_id}")

            await ws.send(json.dumps({
                "message": "请列出你可以用紫鸟浏览器执行哪些亚马逊店铺 RPA 任务（例如广告花费查询、轨迹跟踪表更新）？"
            }))

            # 排空事件流；若触发审批一律 deny，绝不执行真实 RPA
            turn_done = False
            try:
                while True:
                    evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
                    t = evt.get("type")
                    if t in ("done", "error"):
                        turn_done = True
                        break
                    if t == "approval_required":
                        await ws.send(json.dumps({
                            "type": "approval_decision", "decision": "deny",
                        }))
            except (TimeoutError, asyncio.TimeoutError):
                pass
            check("rpa 意图对话回合结束", turn_done, "(25s 内收到 done/error)")

        # 3. 回合后：契约不变（mcp_rpa_* 始终硬隐藏）
        d1 = (await client.get(f"{BASE}/api/tools")).json()
        s1, m1, i1 = _rpa_tools(d1)
        check("回合后仍 3 个 submit_rpa_*", len(s1) == 3, f"submit={s1}")
        check("回合后仍无 mcp_rpa_*（硬隐藏）", not m1, f"mcp_rpa={m1}")
        check("回合后仍无进程内 rpa_*", not i1, str(i1))
        print(f"   [post] count={d1['count']} submit_rpa={len(s1)} mcp_rpa={m1} rpa_*={i1}")

    print(f"\n结果: {'全部通过' if failed == 0 else f'{failed} 项失败'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
