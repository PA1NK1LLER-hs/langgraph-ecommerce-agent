"""WebSocket 端到端冒烟测试 — 验证 REST 认证 + WS 全链路。

覆盖（对应 F11/F1 修复后的新协议）：
  1. POST /api/auth/login 换取 JWT
  2. REST: /api/auth/me、/api/threads、/api/kb/stats（Bearer 认证）
  3. WS: 无 token 连接 → 首条消息 {"type":"auth"} → 期望 auth_ok → thread_id
  4. 发送聊天消息，等待 text 流 + done

用法: .venv/Scripts/python.exe scripts/ws_smoke_test.py
"""
import asyncio
import json
import sys

import httpx
import websockets


async def main() -> None:
    base = "http://localhost:8080"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/api/auth/login",
            json={"username": "claude_verify", "password": "ClaudeVerify123!"},
        )
        if resp.status_code != 200:
            print(f"[FAIL] login: HTTP {resp.status_code} {resp.text}")
            sys.exit(1)
        token = resp.json()["access_token"]
        print(f"[1] login OK (user={resp.json()['username']}, role={resp.json()['role']})")

        headers = {"Authorization": f"Bearer {token}"}
        for label, path in (("me", "/api/auth/me"), ("threads", "/api/threads"), ("kb stats", "/api/kb/stats")):
            r = await client.get(f"{base}{path}", headers=headers)
            ok = r.status_code == 200
            print(f"[REST] {path} -> HTTP {r.status_code} {'OK' if ok else r.text[:100]}")
            if not ok:
                print("[FAIL] REST endpoint failed")
                sys.exit(1)

    # ── WS：新认证流程 —— URL 不带 token，连接后发送 auth 消息（F11）──
    import time as _time

    uri = "ws://localhost:8080/ws/chat"
    events, text_parts = [], []
    try:
        async with websockets.connect(uri, open_timeout=15) as ws:
            print("[2] WS connected (no token in URL)", flush=True)

            # 首条消息认证，期望收到 auth_ok + thread_id（顺序不依赖）
            await ws.send(json.dumps({"type": "auth", "token": token}))
            print("[2b] auth message sent", flush=True)
            auth_ok, thread_id = False, None
            for i in range(3):
                try:
                    t0 = _time.monotonic()
                    evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    dt = _time.monotonic() - t0
                except (TimeoutError, asyncio.TimeoutError):
                    print(f"[2c] recv #{i + 1} TIMEOUT after 15s", flush=True)
                    continue
                print(f"[2d] recv #{i + 1} after {dt:.1f}s: {str(evt)[:150]}", flush=True)
                events.append(evt)
                if evt.get("type") == "auth_ok":
                    auth_ok = evt.get("content") is True
                elif evt.get("type") == "thread_id":
                    thread_id = evt.get("content")
                if auth_ok and thread_id:
                    break
            if not auth_ok:
                print(f"[FAIL] auth_ok not received, events: {events}")
                sys.exit(1)
            if not thread_id:
                print(f"[FAIL] thread_id not received, events: {events}")
                sys.exit(1)
            print(f"[3] auth_ok=True, thread_id={thread_id}", flush=True)

            await ws.send(json.dumps({"message": "你好，请用一句话介绍你自己"}))
            print("[5] message sent, waiting for stream...")
            while len(events) < 30:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
                evt = json.loads(raw)
                events.append(evt)
                if evt.get("type") == "text" or evt.get("content") and isinstance(evt.get("content"), str):
                    text_parts.append(evt.get("content", ""))
                if evt.get("type") in ("done", "error"):
                    break
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        sys.exit(1)

    reply = "".join(text_parts).strip()
    print(f"[6] received {len(events)} events, reply: {reply[:200]!r}")
    if not reply:
        print("[FAIL] no text reply received")
        sys.exit(1)
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
