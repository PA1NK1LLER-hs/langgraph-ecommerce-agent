"""复现"帮我查一下有关豆袋船的信息"卡死问题。

实时打印每个 WS 事件及时间戳，90 秒无事件则判定卡死并退出。
用法: .venv/Scripts/python.exe scripts/repro_doudaichuan.py
"""
import asyncio
import json
import sys
import time

import httpx
import websockets

BASE = "http://localhost:8080"
WS_BASE = "ws://localhost:8080"
T0 = time.monotonic()


def ts() -> str:
    return f"+{time.monotonic() - T0:7.1f}s"


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{BASE}/api/auth/login", json={"username": "claude_verify", "password": "ClaudeVerify123!"},
                              headers={"X-Real-IP": "10.99.0.1"})
        if r.status_code != 200:
            print(f"[FAIL] login {r.status_code} {r.text}"); sys.exit(1)
        token = r.json()["access_token"]
        print(f"{ts()} login OK")

        r = await client.post(f"{BASE}/api/threads", json={"title": "repro-豆袋船"}, headers={"Authorization": f"Bearer {token}"})
        tid = r.json()["thread_id"]
        print(f"{ts()} thread {tid[:8]}")

        ws = await websockets.connect(f"{WS_BASE}/ws/chat?thread_id={tid}", open_timeout=30)
        await ws.send(json.dumps({"type": "auth", "token": token}))
        # 先收掉 auth 流程事件
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            print(f"{ts()} <-- {evt.get('type')} {str(evt.get('content',''))[:50]}")
            if evt.get("type") == "auth_ok":
                break

        await ws.send(json.dumps({"message": "帮我查一下有关豆袋船的信息"}))
        print(f"{ts()} --> 发送查询")

        n = 0
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
            except asyncio.TimeoutError:
                print(f"{ts()} [STALL] 90 秒无任何事件，判定卡死")
                sys.exit(2)
            evt = json.loads(raw)
            n += 1
            t = evt.get("type")
            content = str(evt.get("content", ""))[:80].replace("\n", " ")
            print(f"{ts()} <-- [{n}] {t} {content}")
            if t in ("done", "error"):
                print(f"{ts()} 流结束 ({t})，共 {n} 个事件")
                sys.exit(0 if t == "done" else 3)
        # 无需显式关闭，进程退出即断开


if __name__ == "__main__":
    asyncio.run(main())
