# -*- coding: utf-8 -*-
"""冒烟：RPA 任务队列全链路（DRY_RUN，只读，不触真实业务）。

自包含脚本：
  1. 以 RPA_DRY_RUN=1 在 127.0.0.1:8091 起后端（独立子进程，不动现有 :8080）；
  2. 等 /api/health 就绪，确认日志出现「RPA 调度器已启动」；
  3. 注册/登录冒烟账号（X-Real-IP 防限流）；
  4. 直接向 Postgres 插一条 queued 任务（走 create_rpa_job，无 POST 端点本轮不做）；
  5. 轮询 /api/rpa/jobs/{id} 观察状态流 queued→running→done；
  6. 断言结果含 dry_run，最后关停后端子进程。

DRY_RUN 保证调度器跳过真实 MCP 调用，绝不执行真实业务。

运行：.venv/Scripts/python.exe scripts/test_rpa_queue_smoke.py
"""
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

PORT = 8091
BASE = f"http://127.0.0.1:{PORT}"
LOG_PATH = PROJECT_ROOT / ".data" / "rpa_queue_smoke_backend.log"


def _fake_ip() -> str:
    return ".".join(str(random.randint(1, 254)) for _ in range(4))


def _tail(path: Path, marker: str) -> bool:
    """查看后端日志是否出现某行。"""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return marker in text


async def _wait_ready(client: httpx.AsyncClient, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = await client.get(f"{BASE}/api/health", timeout=5)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    return False


async def _ensure_user(client: httpx.AsyncClient) -> str:
    """登录冒烟账号；不存在则注册。"""
    stamp = str(int(time.time()))
    user = f"rpa_smoke_{stamp}"
    pwd = "RpaSmoke123!"
    r = await client.post(
        f"{BASE}/api/auth/register",
        json={"username": user, "password": pwd},
        headers={"X-Real-IP": _fake_ip()},
    )
    if r.status_code == 200:
        print(f"   [auth] 注册新账号 {user}")
        return r.json()["access_token"]
    r = await client.post(
        f"{BASE}/api/auth/login",
        json={"username": user, "password": pwd},
        headers={"X-Real-IP": _fake_ip()},
    )
    if r.status_code == 200:
        print(f"   [auth] 复用账号 {user}")
        return r.json()["access_token"]
    raise RuntimeError(f"无法登录/注册冒烟账号: HTTP {r.status_code} {r.text[:200]}")


async def main() -> int:
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name} {detail}")
        if not ok:
            failed += 1

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(LOG_PATH, "w", encoding="utf-8")
    env = {**os.environ, "RPA_DRY_RUN": "1"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "src.api.server:app",
        "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "info",
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_f,
        stderr=asyncio.subprocess.STDOUT,
    )
    print(f"[1] 后端已拉起 (pid={proc.pid}, RPA_DRY_RUN=1, :{PORT})")

    try:
        async with httpx.AsyncClient(timeout=15, headers={"X-Real-IP": _fake_ip()}) as client:
            ready = await _wait_ready(client)
            check("后端 /api/health 就绪", ready)
            if not ready:
                print(_tail_log())
                return 1

            await asyncio.sleep(1)  # 让调度器 start() 日志落盘
            check("日志出现「RPA 调度器已启动」", _tail(LOG_PATH, "RPA 调度器已启动"), "(lifespan 挂载调度器)")
            check("日志出现「Database initialized」", _tail(LOG_PATH, "Database initialized"))

            token = await _ensure_user(client)
            auth_headers = {"Authorization": f"Bearer {token}", "X-Real-IP": _fake_ip()}

            # [2] 直接向 DB 插一条 queued 任务（本轮无 POST 端点）
            from api.database import async_session
            from agent.rpa_jobs import create_rpa_job

            async with async_session() as session:
                job = await create_rpa_job(
                    session, "query_campaign_spend", {"start_date": "2026-06-01"}
                )
            job_id = job["job_id"]
            print(f"[2] 已插入任务 {job_id} (query_campaign_spend, queued)")

            # [3] 轮询 API 观察状态流转
            seen: list[str] = []
            final = None
            deadline = time.time() + 40
            while time.time() < deadline:
                r = await client.get(f"{BASE}/api/rpa/jobs/{job_id}", headers=auth_headers)
                if r.status_code != 200:
                    await asyncio.sleep(1)
                    continue
                data = r.json()
                if data["status"] not in seen:
                    seen.append(data["status"])
                    print(f"   [poll] {data['status']}")
                if data["status"] in ("done", "failed"):
                    final = data
                    break
                await asyncio.sleep(1)
            print(f"[3] 状态流: {' → '.join(seen)}")

            # DRY_RUN 下认领→完成仅 ~4ms，API 轮询捕不到 running（瞬时态）。
            # 用调度器日志的「认领」行证明 queued→running→done 全链路真实发生。
            check(
                "调度器日志出现「认领任务 {job_id}」（queued→running）",
                _tail(LOG_PATH, f"RPA 调度器认领任务 {job_id}"),
            )
            check("调度器日志出现「任务完成 {job_id}」（running→done）", _tail(LOG_PATH, f"RPA 任务完成 {job_id}"))
            check("最终状态 done", final and final["status"] == "done", str(final and final["status"]))
            if final:
                result = json.loads(final["result"] or "{}")
                check("结果含 dry_run=true", result.get("dry_run") is True, str(result)[:200])

            # [4] 列表 API
            r = await client.get(f"{BASE}/api/rpa/jobs?limit=5", headers=auth_headers)
            listing = r.json() if r.status_code == 200 else {}
            check("GET /api/rpa/jobs 列表可用", r.status_code == 200 and listing.get("count", 0) >= 1)

    finally:
        # Windows 下 terminate() = TerminateProcess（硬杀），不会走 lifespan 收尾；
        # 这里只验证子进程能被及时回收（若收尾有 anyio cancel-scope 悬挂，
        # proc.wait() 会超时并转 kill，据此判失败）。
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
            reaped = True
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
            reaped = False
        log_f.close()

    check("后端子进程及时回收（无悬挂）", reaped)

    print(f"\n结果: {'全部通过' if failed == 0 else f'{failed} 项失败'}")
    print(f"后端日志: {LOG_PATH}")
    return 0 if failed == 0 else 1


def _tail_log() -> str:
    """读取日志尾部用于诊断。"""
    if not LOG_PATH.exists():
        return "(无日志)"
    return "\n".join(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
