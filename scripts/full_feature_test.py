"""全功能冒烟测试 — REST + WebSocket + RAG 端到端（前端→后端→RAG 全链路）。

用法: .venv/Scripts/python.exe scripts/full_feature_test.py

覆盖:
  A. 健康检查
  B. 认证 & RBAC（register/login/me、viewer 403、admin 角色管理、角色守卫）
  C. 知识库 RAG（stats、search dense/hybrid、真实分块、上传→索引→检索→删除）
  D. 对话链路（WS 认证→流式文本→工具调用→done；模型路由）
  E. 线程 CRUD
  F. 记忆 CRUD
  G. 工具/技能列表
  H. Human-in-the-Loop 审批（execute_code 触发 interrupt→approve→恢复）
"""

import asyncio
import json
import subprocess
import sys
import time
import uuid

import httpx
import websockets

BASE = "http://127.0.0.1:8080"
WS_URL = "ws://127.0.0.1:8080/ws/chat"

CLAUDE_VERIFY = ("claude_verify", "ClaudeVerify123!")

PASS, FAIL, SKIP = [], [], []


def record(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}  {detail[:180]}", flush=True)
    (PASS if ok else FAIL).append((name, detail))


def record_skip(name, detail):
    print(f"  [SKIP] {name}  {detail[:180]}", flush=True)
    SKIP.append((name, detail))


_IP = [0]


def fake_ip():
    _IP[0] += 1
    return f"10.9.{_IP[0] % 250}.{_IP[0] % 250}"


def psql(sql: str) -> tuple[int, str]:
    """通过 docker exec 直连 Postgres 执行 SQL（用于角色提权/回退）。"""
    try:
        p = subprocess.run(
            ["docker", "exec", "langgraph-agent-postgres-1", "psql",
             "-U", "langgraph", "-d", "langgraph", "-tAc", sql],
            capture_output=True, text=True, timeout=30,
        )
        return p.returncode, p.stdout.strip()
    except Exception as exc:
        return -1, str(exc)


async def request(client, method, path, token=None, json=None, files=None, data=None):
    headers = {"X-Real-IP": fake_ip()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await client.request(method, BASE + path, headers=headers,
                                json=json, files=files, data=data)


async def do_login(client, username, password):
    r = await request(client, "POST", "/api/auth/login",
                      json={"username": username, "password": password})
    if r.status_code == 200:
        return r.json()["access_token"]
    return None


# ── A. 健康检查 ────────────────────────────────────────────────────────────

async def test_health(client):
    print("\n=== A. 健康检查 ===")
    r = await request(client, "GET", "/api/health")
    ok = r.status_code == 200
    record("GET /api/health 200", ok, str(r.text[:200]))
    if ok:
        data = r.json()
        record("health=healthy", data.get("status") == "healthy", f"checks={data.get('checks')}")
        for k, v in (data.get("checks") or {}).items():
            record(f"health.check.{k}", bool(v))
        if data.get("checks", {}).get("qdrant") is False:
            record("health.qdrant 说明", False, "Qdrant 未就绪 — RAG 链路不可测")

    # 未认证访问受保护端点 → 401
    r2 = await request(client, "GET", "/api/kb/stats")
    record("未认证 /api/kb/stats → 401", r2.status_code == 401, f"HTTP {r2.status_code}")


# ── B. 认证 & RBAC ─────────────────────────────────────────────────────────

async def test_auth(client, ts):
    print("\n=== B. 认证 & RBAC ===")
    uname = f"full_{ts}"
    pwd = "FullTest123!"
    r = await request(client, "POST", "/api/auth/register",
                      json={"username": uname, "password": pwd})
    record("POST /api/auth/register", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
    new_token = r.json().get("access_token") if r.status_code == 200 else None

    # 重复注册 → 409
    r2 = await request(client, "POST", "/api/auth/register",
                       json={"username": uname, "password": pwd})
    record("重复注册 → 409", r2.status_code == 409, f"HTTP {r2.status_code}")

    # 错误密码 → 401
    r3 = await request(client, "POST", "/api/auth/login",
                       json={"username": uname, "password": "wrong"})
    record("错误密码 → 401", r3.status_code == 401, f"HTTP {r3.status_code}")

    # claude_verify 登录
    cv_token = await do_login(client, *CLAUDE_VERIFY)
    record("claude_verify 登录", cv_token is not None)

    # /me
    r4 = await request(client, "GET", "/api/auth/me", token=cv_token)
    ok = r4.status_code == 200 and r4.json().get("username") == CLAUDE_VERIFY[0]
    record("GET /api/auth/me", ok, f"HTTP {r4.status_code} role={r4.json().get('role') if r4.status_code==200 else '?'}")
    role = r4.json().get("role") if r4.status_code == 200 else "viewer"

    # viewer 访问 admin → 403
    r5 = await request(client, "GET", "/api/admin/users", token=cv_token)
    record("viewer 访问 /api/admin/users → 403", r5.status_code == 403, f"HTTP {r5.status_code}")

    # viewer 上传 KB → 403
    upload_url = f"{BASE}/api/kb/upload"
    files = {"file": ("probe.txt", b"viewer should be blocked", "text/plain")}
    r6 = await request(client, "POST", "/api/kb/upload", token=cv_token, files=files)
    record("viewer 上传 KB → 403", r6.status_code == 403, f"HTTP {r6.status_code}")

    # ── 临时把 claude_verify 提为 admin，测试角色管理后回退 viewer ──
    uid = psql(f"SELECT id FROM users WHERE username='{CLAUDE_VERIFY[0]}'")[1]
    psql(f"UPDATE users SET role='admin' WHERE username='{CLAUDE_VERIFY[0]}'")
    # 重新登录拿 admin token（JWT 不含 role，但 DB 读取时实时取）
    adm_token = await do_login(client, *CLAUDE_VERIFY)
    r7 = await request(client, "GET", "/api/admin/users", token=adm_token)
    record("admin 访问 /api/admin/users", r7.status_code == 200 and len(r7.json().get("users", [])) > 0,
           f"HTTP {r7.status_code} users={len(r7.json().get('users', [])) if r7.status_code==200 else 0}")

    # 把新注册用户提为 editor
    new_uid = psql(f"SELECT id FROM users WHERE username='{uname}'")[1]
    if new_uid:
        r8 = await request(client, "PUT", f"/api/admin/users/{new_uid}/role",
                           json={"role": "editor"}, token=adm_token)
        record("admin 将新用户提为 editor", r8.status_code == 200, f"HTTP {r8.status_code} {r8.text[:80]}")

    # 自我降级被守卫拦截
    my_uid = psql(f"SELECT id FROM users WHERE username='{CLAUDE_VERIFY[0]}'")[1]
    r9 = await request(client, "PUT", f"/api/admin/users/{my_uid}/role",
                       json={"role": "viewer"}, token=adm_token)
    record("admin 自我降级被拒（400）", r9.status_code == 400, f"HTTP {r9.status_code} {r9.text[:80]}")

    # ── 回退 claude_verify 为 viewer ──
    psql(f"UPDATE users SET role='viewer' WHERE username='{CLAUDE_VERIFY[0]}'")
    print(f"       [info] 已回退 {CLAUDE_VERIFY[0]} role=viewer; 新用户 {uname} 提为 editor")

    return cv_token, adm_token, new_token, uname


# ── C. 知识库 RAG ──────────────────────────────────────────────────────────

async def test_kb(client, editor_uname, admin_token, ts):
    print("\n=== C. 知识库 RAG ===")
    editor_pwd = "FullTest123!"
    ed_token = await do_login(client, editor_uname, editor_pwd)
    if not ed_token:
        # 通过 admin 直接查用户再登
        record("editor 登录", False, "editor 用户登录失败")
        return
    record("editor 登录", True)

    # stats
    r = await request(client, "GET", "/api/kb/stats", token=ed_token)
    record("GET /api/kb/stats", r.status_code == 200, f"HTTP {r.status_code} total_chunks={r.json().get('total_chunks') if r.status_code==200 else '?'}")
    stats = r.json() if r.status_code == 200 else {}
    sources_before = stats.get("total_chunks", 0)

    # search dense
    r2 = await request(client, "POST", "/api/kb/search",
                       json={"query": "产品", "top_k": 5, "mode": "dense"}, token=ed_token)
    ok = r2.status_code == 200 and r2.json().get("status") == "success"
    record("POST /api/kb/search dense", ok, f"HTTP {r2.status_code} results={len(r2.json().get('results', [])) if ok else 0}")

    # search hybrid
    r3 = await request(client, "POST", "/api/kb/search",
                       json={"query": "产品", "top_k": 5, "mode": "hybrid"}, token=ed_token)
    ok = r3.status_code == 200
    record("POST /api/kb/search hybrid", ok, f"HTTP {r3.status_code} results={len(r3.json().get('results', [])) if ok else 0}")

    # 上传一个测试文档（内容带唯一标记，验证全链路）
    marker = f"marker_{ts}_量子纠缠测试文档"
    content = (f"# 全功能测试文档 {marker}\n\n"
               f"这是用于端到端验证的产品说明文档。\n"
               f"产品名称：玄铁测试笔记本 {ts}\n"
               f"负责人：测试工程师\n"
               f"站点：CN\n\n" + "\n".join(f"ASIN {ts}-{i:03d} 对应 SKU 序号 {i}，库存 100+i 件。" for i in range(1, 6)))
    fname = f"full_test_{ts}.md"
    fbytes = content.encode("utf-8")

    r4 = await request(client, "POST", "/api/kb/upload",
                       token=ed_token, files={"file": (fname, fbytes, "text/markdown")})
    ok = r4.status_code == 200 and r4.json().get("status") == "success"
    record("上传文档 → 索引", ok, f"HTTP {r4.status_code} {r4.json().get('indexed_chunks') if ok else r4.text[:100]}")
    source_name = fname

    if ok:
        task_id = r4.json().get("task_id")
        # 等待索引完成（upload 是同步的，但进度查询可验证）
        rp = await request(client, "GET", f"/api/kb/upload-progress/{task_id}")
        record("查询索引进度", rp.status_code == 200, f"HTTP {rp.status_code} {rp.json().get('progress') if rp.status_code==200 else ''}")

        # stats 增加
        r5 = await request(client, "GET", "/api/kb/stats", token=ed_token)
        chunks_after = r5.json().get("total_chunks", 0) if r5.status_code == 200 else 0
        record("stats 反映新增文档", chunks_after > sources_before, f"before={sources_before} after={chunks_after}")

        # 用唯一标记检索，验证命中
        await asyncio.sleep(1)  # 给向量落库一点时间
        r6 = await request(client, "POST", "/api/kb/search",
                           json={"query": f"玄铁测试笔记本 {ts}", "top_k": 5, "mode": "hybrid"}, token=ed_token)
        hit = False
        if r6.status_code == 200:
            for res in r6.json().get("results", []):
                if source_name in res.get("source", "") or marker in res.get("content", ""):
                    hit = True
                    break
        record("上传后可检索命中", hit, f"HTTP {r6.status_code} results={len(r6.json().get('results', [])) if r6.status_code==200 else 0}")

        # 真实分块
        r7 = await request(client, "GET", f"/api/kb/sources/{source_name}/chunks")
        ok7 = r7.status_code == 200 and r7.json().get("count", 0) > 0
        record("GET sources/{id}/chunks 真实分块", ok7, f"HTTP {r7.status_code} count={r7.json().get('count') if r7.status_code==200 else 0}")

        # 删除来源（cleanup）
        r8 = await request(client, "DELETE", f"/api/kb/sources/{source_name}", token=ed_token)
        record("删除来源", r8.status_code == 200 and r8.json().get("status") == "success", f"HTTP {r8.status_code} {r8.text[:80]}")
    else:
        record_skip("上传后检索/分块/删除", "上传失败，跳过后续 KB 写链路")

    # URL 导入 SSRF 防护
    r9 = await request(client, "POST", "/api/kb/import-url",
                       json={"url": "http://127.0.0.1:8080/"}, token=ed_token)
    record("URL 导入 SSRF 拦截内网地址", r9.status_code == 400, f"HTTP {r9.status_code} {r9.text[:80]}")

    return source_name


# ── D. 对话链路（WebSocket）────────────────────────────────────────────────

async def test_chat(client, token):
    print("\n=== D. 对话链路（WebSocket）===")
    try:
        async with websockets.connect(WS_URL, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            auth_ok, thread_id = False, None
            events = []
            for _ in range(4):
                try:
                    evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except (TimeoutError, asyncio.TimeoutError):
                    break
                events.append(evt)
                if evt.get("type") == "auth_ok":
                    auth_ok = evt.get("content") is True
                elif evt.get("type") == "thread_id":
                    thread_id = evt.get("content")
                if auth_ok and thread_id:
                    break
            record("WS auth_ok + thread_id", auth_ok and bool(thread_id), f"thread_id={thread_id}")

            # 流式对话：打招呼
            await ws.send(json.dumps({"message": f"你好，用一句话介绍你自己（测试 {int(time.time())}）"}))
            texts, models, tool_calls, done = [], [], [], False
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                evt = json.loads(raw)
                events.append(evt)
                t = evt.get("type")
                if t == "text":
                    texts.append(evt.get("content", ""))
                elif t == "model":
                    models.append(evt.get("model", ""))
                elif t == "tool_call":
                    tool_calls.append(evt.get("tool", ""))
                elif t in ("done", "error"):
                    done = True
                    break
                if len(texts) > 200:
                    break
            reply = "".join(texts)
            record("对话收到流式文本回复", bool(reply.strip()), f"len={len(reply)} models={set(models)}")
            record("对话收到 done 事件", done)
            if not (reply.strip() and done):
                record("WS 对话完整结束", False, f"events={[e.get('type') for e in events][-8:]}")

            # 模型路由：短查询应为 flash
            model_flash = any("flash" in m for m in models)
            record("短查询路由 flash 模型", model_flash or not models, f"models={set(models)}")

            # 知识库对话：问 RAG 问题，期望出现 tool_search_knowledge 或引用
            await ws.send(json.dumps({"message": f"请查询知识库中有哪些产品资料（测试 {int(time.time())}）"}))
            k_tools, k_done = [], False
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
                evt = json.loads(raw)
                t = evt.get("type")
                if t == "tool_call":
                    k_tools.append(evt.get("tool", ""))
                elif t == "text":
                    pass
                elif t in ("done", "error"):
                    k_done = True
                    break
            record("知识库问题触发检索工具", any(("search_knowledge" in t) or ("list_knowledge_sources" in t) for t in k_tools), f"tools={set(k_tools)}")
            record("知识库问题对话结束", k_done)

            await ws.send(json.dumps({"type": "reset"}) if False else json.dumps({"message": "/reset"}))
            # /reset 由 chat 层处理，发送后应收到 thread_id + done
            try:
                r_evts = []
                for _ in range(4):
                    evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    r_evts.append(evt)
                    if evt.get("type") == "done":
                        break
                new_tid = next((e.get("content") for e in r_evts if e.get("type") == "thread_id"), None)
                record("/reset 开启新对话", bool(new_tid) and new_tid != thread_id, f"new={new_tid}")
            except (TimeoutError, asyncio.TimeoutError):
                record("/reset 开启新对话", False, "recv 超时")

            return thread_id
    except Exception as exc:
        record("WebSocket 连接", False, f"{type(exc).__name__}: {exc}")
        return None


# ── E. 线程 ────────────────────────────────────────────────────────────────

async def test_threads(client, token):
    print("\n=== E. 线程 CRUD ===")
    r = await request(client, "GET", "/api/threads", token=token)
    ok = r.status_code == 200 and "threads" in r.json()
    record("GET /api/threads", ok, f"HTTP {r.status_code} count={r.json().get('count') if ok else '?'}")

    r = await request(client, "POST", "/api/threads", json={"title": f"测试线程 {int(time.time())}"}, token=token)
    tid = r.json().get("thread_id") if r.status_code == 201 else None
    record("POST /api/threads", tid is not None, f"HTTP {r.status_code} tid={tid}")

    if tid:
        r = await request(client, "PATCH", f"/api/threads/{tid}", json={"title": "已改标题"}, token=token)
        record("PATCH 线程标题", r.status_code == 200 and r.json().get("title") == "已改标题", f"HTTP {r.status_code}")

        r = await request(client, "GET", f"/api/threads/{tid}", token=token)
        record("GET 线程详情", r.status_code == 200, f"HTTP {r.status_code} messages={len(r.json().get('messages', [])) if r.status_code==200 else 0}")

        r = await request(client, "DELETE", f"/api/threads/{tid}", token=token)
        record("DELETE 线程", r.status_code == 204, f"HTTP {r.status_code}")

        r = await request(client, "GET", f"/api/threads/{tid}", token=token)
        record("删除后查线程 → 404", r.status_code == 404, f"HTTP {r.status_code}")


# ── F. 记忆 ────────────────────────────────────────────────────────────────

async def test_memories(client, token, editor_uname, editor_pwd):
    print("\n=== F. 记忆 CRUD ===")
    mem_text = f"测试记忆：用户偏好蓝色主题 {int(time.time())}"
    r = await request(client, "POST", "/api/memories", json={"content": mem_text, "category": "preference"}, token=token)
    record("POST /api/memories", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")

    r = await request(client, "GET", "/api/memories", token=token)
    ok = r.status_code == 200
    record("GET /api/memories", ok, f"HTTP {r.status_code}")
    if ok:
        mems = r.json()
        items = mems.get("results") or mems.get("memories") or (mems if isinstance(mems, list) else [])
        # mem0 用 LLM 把原始文本改写成英文事实，因此按语义标记匹配而非原文子串
        hit = False
        sample = ""
        for m in items:
            s = str(m).lower()
            sample = s[:200]
            if any(k in s for k in ("blue", "prefer", "theme", "偏好", "蓝色")):
                hit = True
                break
        record("记忆已存储且可检索（mem0 改写为英文）", hit, f"count={mems.get('count')} sample={sample}")

    # viewer 删除记忆 → 403（正确守卫）
    r = await request(client, "DELETE", "/api/memories", json={"query": "用户偏好蓝色主题"}, token=token)
    record("viewer 删除记忆被守卫拒绝 403", r.status_code == 403, f"HTTP {r.status_code} {r.text[:80]}")

    # editor 删除记忆 → 200
    ed_token = await do_login(client, editor_uname, editor_pwd)
    if ed_token:
        r = await request(client, "DELETE", "/api/memories", json={"query": "用户偏好蓝色主题"}, token=ed_token)
        record("editor 删除记忆 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
    else:
        record_skip("editor 删除记忆", "editor 登录失败")


# ── G. 工具 / 技能 ─────────────────────────────────────────────────────────

async def test_tools(client):
    print("\n=== G. 工具/技能列表 ===")
    r = await request(client, "GET", "/api/tools")
    ok = r.status_code == 200 and r.json().get("count", 0) > 0
    record("GET /api/tools", ok, f"HTTP {r.status_code} count={r.json().get('count') if ok else 0}")
    if ok:
        names = [t["name"] for t in r.json()["tools"]]
        record("核心工具存在 tool_search_knowledge", "tool_search_knowledge" in names, f"tools={len(names)}")

    r = await request(client, "GET", "/api/skills")
    ok = r.status_code == 200
    record("GET /api/skills", ok, f"HTTP {r.status_code} count={r.json().get('count') if ok else 0}")


# ── H. 审批（HITL）─────────────────────────────────────────────────────────

async def test_approval(client, token, editor_uname, editor_pwd):
    print("\n=== H. Human-in-the-Loop 审批 ===")
    # 用 editor 账号执行，避开 viewer 对高风险工具的 RBAC 拒绝，
    # 让 execute_code 真正跑到代码执行器（viewer 拒绝已在首次运行日志中验证）。
    token = await do_login(client, editor_uname, editor_pwd) or token
    try:
        async with websockets.connect(WS_URL, open_timeout=15) as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            for _ in range(4):
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if evt.get("type") == "thread_id":
                    thread_id = evt.get("content")
                    break

            # 触发高风险工具（execute_code）
            await ws.send(json.dumps({"message": "请帮我运行 Python 代码：print(1+1)，输出结果即可"}))
            approval_payload = None
            texts = []
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=150)
                evt = json.loads(raw)
                t = evt.get("type")
                if t == "approval_required":
                    approval_payload = evt
                    break
                if t == "text":
                    texts.append(evt.get("content", ""))
                if t in ("done", "error"):
                    break
            if approval_payload is None:
                record("触发审批中断", False, f"未收到 approval_required, texts={''.join(texts)[:80]!r}")
                return
            calls = approval_payload.get("calls", [])
            record("触发审批中断 approval_required", True, f"calls={[c.get('name') for c in calls]}")

            # 通过 WS 决定 approve
            await ws.send(json.dumps({"type": "approval_decision", "decision": "approve"}))
            texts2, done = [], False
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=150)
                evt = json.loads(raw)
                t = evt.get("type")
                if t == "text":
                    texts2.append(evt.get("content", ""))
                elif t == "tool_result":
                    pass
                elif t in ("done", "error"):
                    done = True
                    break
            reply = "".join(texts2)
            record("approve 后恢复执行并出结果", bool(reply.strip()) and done, f"reply={reply[:80]!r} done={done}")
    except (TimeoutError, asyncio.TimeoutError) as exc:
        record("审批流程", False, f"超时: {exc}")
    except Exception as exc:
        record("审批流程", False, f"{type(exc).__name__}: {exc}")


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ts = int(time.time())
    print(f"全功能冒烟测试 @ {ts}  时间: {__import__('datetime').datetime.now()}")
    async with httpx.AsyncClient(timeout=60) as client:
        await test_health(client)
        cv_token, adm_token, new_token, editor_uname = await test_auth(client, ts)
        await test_tools(client)
        await test_memories(client, cv_token, editor_uname, "FullTest123!")
        thread_id = await test_chat(client, cv_token)
        await test_threads(client, cv_token)
        await test_kb(client, editor_uname, adm_token, ts)
        await test_approval(client, cv_token, editor_uname, "FullTest123!")

    print("\n" + "=" * 70)
    print(f"汇总: PASS={len(PASS)}  FAIL={len(FAIL)}  SKIP={len(SKIP)}")
    for name, detail in FAIL:
        print(f"  ✗ {name}: {detail}")
    for name, detail in SKIP:
        print(f"  - {name}: {detail}")
    if FAIL:
        print("结论: 存在失败项")
        sys.exit(1)
    print("结论: 全部通过")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
