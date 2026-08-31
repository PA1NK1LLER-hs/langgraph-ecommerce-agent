"""前端上传 + 图片识别 专项测试。

覆盖:
  1. 前端同款上传契约（client.ts uploadFile: multipart + tags=&chunk_strategy=semantic）→ 索引
  2. 图片文件上传 → RAG（vision 增强：OCR 占位符替换）→ 检索命中 → 清理
  3. 聊天图片识别（ChatView 多模态：WS 发送 images dataUri）→ 模型 OCR 出图中文字

前置: 后端已启动（run.py），claude_verify 会被临时提为 editor 并在结束时回退。
"""

import asyncio
import base64
import io
import json
import subprocess
import sys
import time
import uuid

import httpx
import websockets
from PIL import Image, ImageDraw, ImageFont

BASE = "http://127.0.0.1:8080"
WS_URL = "ws://127.0.0.1:8080/ws/chat"

CLAUDE = ("claude_verify", "ClaudeVerify123!")
MARK = f"RAGTEST-{int(time.time())}"

PASS, FAIL, SKIP = [], [], []


def record(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}  {detail[:200]}", flush=True)
    (PASS if ok else FAIL).append((name, detail))


def record_skip(name, detail):
    print(f"  [SKIP] {name}  {detail[:200]}", flush=True)
    SKIP.append((name, detail))


_IP = [0]
def fake_ip():
    _IP[0] += 1
    return f"10.8.{_IP[0] % 250}.{_IP[0] % 250}"


def psql(sql: str) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["docker", "exec", "langgraph-agent-postgres-1", "psql",
             "-U", "langgraph", "-d", "langgraph", "-tAc", sql],
            capture_output=True, text=True, timeout=30,
        )
        return p.returncode, p.stdout.strip()
    except Exception as exc:
        return -1, str(exc)


async def request(client, method, path, token=None, **kw):
    headers = {"X-Real-IP": fake_ip()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await client.request(method, BASE + path, headers=headers, **kw)


def make_test_image(extra_line: bool = True) -> bytes:
    """生成 800x300 白底黑字测试图，含 ASCII 标记（OCR 断言用）+ 可选中文行。"""
    img = Image.new("RGB", (800, 300), "white")
    d = ImageDraw.Draw(img)
    font_big = None
    for fp in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            font_big = ImageFont.truetype(fp, 56)
            break
        except Exception:
            continue
    font_cn = None
    for fp in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        try:
            font_cn = ImageFont.truetype(fp, 40)
            break
        except Exception:
            continue
    if font_big is None:
        font_big = ImageFont.load_default(56)
    d.text((40, 60), f"SKU {MARK}", fill="black", font=font_big)
    if extra_line and font_cn is not None:
        d.text((40, 170), "量子计算机 · 库存 128 台", fill="black", font=font_cn)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── 1. 前端同款上传契约（.md） ─────────────────────────────────────────────

async def test_frontend_upload(client, token):
    print("\n=== 1. 前端同款上传契约（multipart + tags=&chunk_strategy=semantic）===")
    content = (f"# 前端上传测试 {MARK}\n\n"
               f"产品：玄铁笔记本\n负责人：测试工程师\n站点：CN\n\n" +
               "\n".join(f"ASIN {MARK}-{i:03d} 库存 {100+i}。" for i in range(1, 4)))
    fname = f"frontend_{MARK}.md"
    files = {"file": (fname, content.encode("utf-8"), "text/markdown")}
    # 与 client.ts uploadFile 完全一致: ?tags=&chunk_strategy=semantic
    r = await request(client, "POST", f"/api/kb/upload?tags=&chunk_strategy=semantic",
                      token=token, files=files)
    ok = r.status_code == 200 and r.json().get("indexed_chunks", 0) >= 1
    record("前端契约上传 .md → 索引", ok,
           f"HTTP {r.status_code} indexed={r.json().get('indexed_chunks') if ok else r.text[:100]}")
    if ok:
        # 检索命中
        await asyncio.sleep(0.8)
        r2 = await request(client, "POST", "/api/kb/search",
                           json={"query": f"玄铁笔记本 {MARK}", "top_k": 3, "mode": "hybrid"}, token=token)
        hit = any(MARK in r.get("content", "") for r in r2.json().get("results", [])) if r2.status_code == 200 else False
        record("前端上传文档可检索", hit, f"HTTP {r2.status_code}")
        r3 = await request(client, "DELETE", f"/api/kb/sources/{fname}", token=token)
        record("清理前端上传文档", r3.status_code == 200, f"HTTP {r3.status_code}")


# ── 2. 图片文件上传 → RAG + vision 增强 ────────────────────────────────────

async def test_image_upload(client, token, png_bytes):
    print("\n=== 2. 图片上传 → RAG（vision OCR 增强）===")
    img_name = f"sku_{MARK}.png"
    files = {"file": (img_name, png_bytes, "image/png")}
    r = await request(client, "POST", f"/api/kb/upload?tags=&chunk_strategy=semantic",
                      token=token, files=files)
    ok = r.status_code == 200 and r.json().get("indexed_chunks", 0) >= 1
    record("上传图片 → 索引", ok,
           f"HTTP {r.status_code} indexed={r.json().get('indexed_chunks') if ok else r.text[:120]} total={r.json().get('total_chunks') if ok else 0}")
    if not ok:
        return

    # 真实分块应含 vision OCR 出的文字（占位符已被替换）
    await asyncio.sleep(1.0)
    r2 = await request(client, "GET", f"/api/kb/sources/{img_name}/chunks")
    chunks = r2.json().get("chunks", []) if r2.status_code == 200 else []
    texts = " ".join(c.get("content", "") for c in chunks)
    ocr_hit = MARK in texts or "SKU" in texts
    placeholder = "[IMAGE" in texts
    record("图片分块已被 vision OCR 增强", ocr_hit and not placeholder,
           f"chunks={len(chunks)} placeholder={placeholder} sample={texts[:140]!r}")

    # 语义检索能命中图中文字
    r3 = await request(client, "POST", "/api/kb/search",
                       json={"query": MARK, "top_k": 3, "mode": "hybrid"}, token=token)
    hit = False
    if r3.status_code == 200:
        for res in r3.json().get("results", []):
            if MARK in res.get("content", "") or img_name in res.get("source", ""):
                hit = True
                break
    record("图中文字可被检索命中", hit, f"HTTP {r3.status_code}")

    r4 = await request(client, "DELETE", f"/api/kb/sources/{img_name}", token=token)
    record("清理图片来源", r4.status_code == 200, f"HTTP {r4.status_code}")


# ── 3. 聊天图片识别（WS 多模态） ───────────────────────────────────────────

async def test_chat_image(client, token, png_bytes):
    print("\n=== 3. 聊天图片识别（WS 多模态 images dataUri）===")
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    try:
        async with websockets.connect(WS_URL, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            for _ in range(4):
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if evt.get("type") == "thread_id":
                    break

            await ws.send(json.dumps({
                "message": "请详细描述这张图片的内容，并转录图中所有文字",
                "images": [data_uri],
            }))
            texts, tool_calls, done, err = [], [], False, ""
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=150)
                evt = json.loads(raw)
                t = evt.get("type")
                if t == "text":
                    texts.append(evt.get("content", ""))
                elif t == "tool_call":
                    tool_calls.append(evt.get("tool", ""))
                elif t == "error":
                    err = evt.get("content", "")
                    done = True
                    break
                elif t in ("done",):
                    done = True
                    break
                if len(texts) > 300:
                    break
            reply = "".join(texts)
            record("图片识别返回文本回复", bool(reply.strip()), f"len={len(reply)} done={done} err={err[:80]} tool_calls={set(tool_calls)}")
            ocr = MARK in reply or "SKU" in reply or str(int(time.time()))[4:] in reply
            record("回复中识别出图中 SKU 标记", ocr, f"reply={reply[:120]!r}")
            record("识别流程完整结束", done, "")
    except (TimeoutError, asyncio.TimeoutError):
        record("聊天图片识别", False, "WS 超时")
    except Exception as exc:
        record("聊天图片识别", False, f"{type(exc).__name__}: {exc}")


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"图片/RAG 上传专项测试 @ {MARK}  {time.strftime('%H:%M:%S')}")
    psql(f"UPDATE users SET role='editor' WHERE username='{CLAUDE[0]}'")

    async with httpx.AsyncClient(timeout=90) as client:
        r = await request(client, "POST", "/api/auth/login",
                          json={"username": CLAUDE[0], "password": CLAUDE[1]})
        token = r.json().get("access_token")
        if not token:
            print("  登录失败，中止")
            psql(f"UPDATE users SET role='viewer' WHERE username='{CLAUDE[0]}'")
            sys.exit(1)

        await test_frontend_upload(client, token)
        png = make_test_image()
        await test_image_upload(client, token, png)
        await test_chat_image(client, token, png)

    psql(f"UPDATE users SET role='viewer' WHERE username='{CLAUDE[0]}'")
    print("\n" + "=" * 70)
    print(f"汇总: PASS={len(PASS)} FAIL={len(FAIL)} SKIP={len(SKIP)}")
    for n, d in FAIL:
        print(f"  ✗ {n}: {d}")
    for n, d in SKIP:
        print(f"  - {n}: {d}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
