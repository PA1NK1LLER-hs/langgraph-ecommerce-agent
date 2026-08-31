# -*- coding: utf-8 -*-
"""真实 RPA 冒烟：DB 直插任务 → 后台调度器 → 真实 RPA MCP 执行器 → 状态轮询。

默认跑 query_campaign_spend（广告花费查询：读数据 + 生成报表文件，最安全）。
可用参数覆盖日期：
    .venv/Scripts/python.exe scripts/test_rpa_real_smoke.py 2026-08-28 [2026-08-28]

⚠️ 真实业务执行，时长 5~15 分钟，须在用户明确授权下运行。
"""
import asyncio
import json
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

BASE = "http://127.0.0.1:8080"
FAKE_IP = "203.0.113.7"


async def main() -> int:
    start_date = sys.argv[1] if len(sys.argv) > 1 else "2026-08-28"
    end_date = sys.argv[2] if len(sys.argv) > 2 else ""

    # [1] DB 直插真实任务（本轮无 POST 端点；调度器 2s 内认领）
    from api.database import async_session
    from agent.rpa_jobs import create_rpa_job

    async with async_session() as session:
        job = await create_rpa_job(
            session, "query_campaign_spend",
            {"start_date": start_date, "end_date": end_date, "output_dir": ""},
        )
    job_id = job["job_id"]
    print(f"[submit] job_id={job_id} query_campaign_spend {start_date}..{end_date or start_date}")

    # [2] 登录（冒烟账号）拿 token，走公开 API 轮询（/rpa 面板同链路）
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{BASE}/api/auth/login",
            json={"username": "claude_verify", "password": "ClaudeVerify123!"},
            headers={"X-Real-IP": FAKE_IP},
        )
        if r.status_code != 200:
            print(f"[auth] 登录失败 HTTP {r.status_code}: {r.text[:200]}")
            return 1
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "X-Real-IP": FAKE_IP}
        print("[auth] 登录成功")

        # [3] 轮询直到 done/failed（25 分钟上限）
        seen: list[str] = []
        deadline = time.time() + 25 * 60
        while time.time() < deadline:
            rr = await client.get(f"{BASE}/api/rpa/jobs/{job_id}", headers=headers)
            if rr.status_code == 200:
                d = rr.json()
                if d["status"] not in seen:
                    seen.append(d["status"])
                    print(f"[poll] {time.strftime('%H:%M:%S')} status={d['status']}")
                if d["status"] in ("done", "failed"):
                    print(f"[final] 状态流: {' → '.join(seen)}")
                    print(json.dumps(d, ensure_ascii=False, indent=2))
                    return 0 if d["status"] == "done" else 1
            await asyncio.sleep(10)

    print("[timeout] 25 分钟未完成")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
