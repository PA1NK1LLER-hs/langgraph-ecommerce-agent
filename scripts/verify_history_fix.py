"""历史对话 checkpoint 落盘验证 — 覆盖两种场景：

  1. 正常流程：聊天完成 → GET /api/threads/{id} 应包含用户消息 + 助手回复
  2. 断连流程：流式生成中途断开 WS → 后台图继续跑完 → 历史仍包含助手回复
     （修复前：断连取消 agent 节点，checkpoint 写入 CancelledError，回复丢失）

用法: .venv/Scripts/python.exe scripts/verify_history_fix.py
"""
import asyncio
import json
import sys

import httpx
import websockets

BASE = "http://localhost:8080"
WS_BASE = "ws://localhost:8080"
USERNAME = "claude_verify"
PASSWORD = "ClaudeVerify123!"


async def login(client: httpx.AsyncClient) -> str:
    resp = await client.post(f"{BASE}/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    if resp.status_code != 200:
        print(f"[FAIL] login: HTTP {resp.status_code} {resp.text}")
        sys.exit(1)
    return resp.json()["access_token"]


async def create_thread(client: httpx.AsyncClient, token: str, title: str) -> str:
    resp = await client.post(
        f"{BASE}/api/threads",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 201:
        print(f"[FAIL] create thread: HTTP {resp.status_code} {resp.text}")
        sys.exit(1)
    return resp.json()["thread_id"]


async def ws_handshake(thread_id: str, token: str) -> websockets.WebSocketClientProtocol:
    ws = await websockets.connect(f"{WS_BASE}/ws/chat?thread_id={thread_id}", open_timeout=30)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    while True:
        evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        if evt.get("type") == "auth_ok":
            if not evt.get("content"):
                raise RuntimeError("auth_ok: false")
            return ws
        if evt.get("type") == "error":
            raise RuntimeError(f"WS error during auth: {evt}")


async def get_messages(client: httpx.AsyncClient, token: str, thread_id: str) -> list[dict]:
    resp = await client.get(
        f"{BASE}/api/threads/{thread_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"GET thread: HTTP {resp.status_code} {resp.text}")
    return resp.json()["messages"]


async def test_normal_flow(client: httpx.AsyncClient, token: str) -> bool:
    print("\n=== [1] 正常流程 ===")
    tid = await create_thread(client, token, "verify-normal")
    ws = await ws_handshake(tid, token)
    await ws.send(json.dumps({"message": "请用一句话介绍你自己，不要调用任何工具"}))
    text_seen = 0
    try:
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            t = evt.get("type")
            if t == "text":
                text_seen += 1
            elif t == "done":
                break
            elif t == "error":
                raise RuntimeError(f"stream error: {evt}")
    finally:
        await ws.close()

    msgs = await get_messages(client, token, tid)
    roles = [m["role"] for m in msgs]
    has_user = any(m["role"] == "user" for m in msgs)
    has_assistant = any(m["role"] == "assistant" and m.get("content") for m in msgs)
    ok = has_user and has_assistant and text_seen > 0
    print(f"  text 事件 {text_seen} 个; 历史消息 roles={roles}; 用户消息={'Y' if has_user else 'N'}, 助手回复={'Y' if has_assistant else 'N'}")
    print(f"  [{'PASS' if ok else 'FAIL'}] 正常流程历史完整")
    return ok


async def test_disconnect_flow(client: httpx.AsyncClient, token: str) -> bool:
    print("\n=== [2] 生成中途断连流程 ===")
    tid = await create_thread(client, token, "verify-disconnect")
    ws = await ws_handshake(tid, token)
    # 一个需要较多 token 的问题，确保首个 token 后仍有一段生成时间
    await ws.send(json.dumps({
        "message": "请写一篇 300 字左右的短文，介绍人工智能的发展历程，分三段，不要调用工具"
    }))
    text_seen = 0
    try:
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            t = evt.get("type")
            if t == "text":
                text_seen += 1
                break  # 收到首个 token 立即断开，模拟用户切换线程/刷新页面
            elif t == "done" or t == "error":
                break
    except (asyncio.TimeoutError, Exception) as exc:
        print(f"  (recv 结束: {type(exc).__name__})")
    await ws.close()
    print(f"  已断开（收到 {text_seen} 个 token 后）")

    # 轮询历史接口：后台图完成后助手回复应出现在 checkpoint 中
    msgs = []
    for attempt in range(30):
        await asyncio.sleep(3)
        msgs = await get_messages(client, token, tid)
        if any(m["role"] == "assistant" and m.get("content") for m in msgs):
            break
        print(f"  等待后台生成完成… (第 {attempt + 1} 次轮询, 当前 {len(msgs)} 条)")

    roles = [m["role"] for m in msgs]
    has_assistant = any(m["role"] == "assistant" and m.get("content") for m in msgs)
    ok = has_assistant
    print(f"  断连后历史 roles={roles}; 助手回复={'Y' if has_assistant else 'N'}")
    print(f"  [{'PASS' if ok else 'FAIL'}] 断连后回复仍写入 checkpoint")
    return ok


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        token = await login(client)
        ok1 = await test_normal_flow(client, token)
        ok2 = await test_disconnect_flow(client, token)
        print("\n==============================")
        print(f"正常流程: {'PASS' if ok1 else 'FAIL'} | 断连流程: {'PASS' if ok2 else 'FAIL'}")
        sys.exit(0 if (ok1 and ok2) else 1)


if __name__ == "__main__":
    asyncio.run(main())
