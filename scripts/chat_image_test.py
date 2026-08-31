"""聊天图片识别复测 — 验证「图片消息直进 agent」路由修复。

两条提示词都要求返回图内文字，修复后都应直接回答（无规划器劫持）：
  P1: 请读出这张图片中的所有文字           → 本就走直接路径
  P2: 请详细描述这张图片的内容，并转录图中所有文字  → 修复前被判 complex 走规划器
"""

import asyncio
import base64
import io
import json
import sys
import time
import uuid

import httpx
import websockets
from PIL import Image, ImageDraw, ImageFont

BASE = "http://127.0.0.1:8080"
WS_URL = "ws://127.0.0.1:8080/ws/chat"
CLAUDE = ("claude_verify", "ClaudeVerify123!")

PASS, FAIL = [], []


def record(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail[:200]}", flush=True)
    (PASS if ok else FAIL).append((name, detail))


def make_image() -> tuple[str, str]:
    marker = f"SKU-CHAT-{int(time.time())}"
    img = Image.new("RGB", (800, 300), "white")
    d = ImageDraw.Draw(img)
    font = None
    for fp in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            font = ImageFont.truetype(fp, 56)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default(56)
    d.text((40, 60), marker, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return marker, "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def chat_once(ws, prompt, data_uri):
    await ws.send(json.dumps({"message": prompt, "images": [data_uri]}))
    texts, tools, done, err = [], set(), False, ""
    t0 = time.time()
    while time.time() - t0 < 150:
        try:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=150))
        except Exception:
            break
        ty = evt.get("type")
        if ty == "text":
            texts.append(evt.get("content", ""))
        elif ty == "tool_call":
            tools.add(evt.get("tool", ""))
        elif ty == "error":
            err = evt.get("content", "")
            done = True
            break
        elif ty == "done":
            done = True
            break
        if len(texts) > 400:
            break
    return "".join(texts), tools, done, err


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    marker, data_uri = make_image()
    print(f"聊天图片识别复测 @ {marker}")
    r = httpx.post(f"{BASE}/api/auth/login",
                   json={"username": CLAUDE[0], "password": CLAUDE[1]},
                   headers={"X-Real-IP": "10.7.2.1"})
    token = r.json()["access_token"]

    async with websockets.connect(WS_URL, open_timeout=15) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        for _ in range(4):
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if evt.get("type") == "thread_id":
                break

        for label, prompt in [
            ("P1 直白提示词", "请读出这张图片中的所有文字"),
            ("P2 描述式提示词（修复前走规划器）", "请详细描述这张图片的内容，并转录图中所有文字"),
        ]:
            reply, tools, done, err = await chat_once(ws, prompt, data_uri)
            ocr = marker in reply
            plan = "步骤" in reply[:60] or '"steps"' in reply[:80]
            record(f"{label}: 直接回答且 OCR 命中", ocr and not plan and done,
                   f"done={done} tools={tools} plan_json={plan} reply={reply[:100]!r}")
            # 每条消息单独新会话，避免上下文串扰
            await ws.send(json.dumps({"message": "/reset"}))
            for _ in range(4):
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if evt.get("type") == "done":
                    break

    print("\n" + "=" * 60)
    print(f"汇总: PASS={len(PASS)} FAIL={len(FAIL)}")
    for n, d in FAIL:
        print(f"  ✗ {n}: {d}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
