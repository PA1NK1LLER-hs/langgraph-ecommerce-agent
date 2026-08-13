"""知识库更新链路端到端验证 — 删旧→导新 是否真正更新对应关系。

覆盖：
  1. 上传 v1 文档（供应商A → 产品B）
  2. Neo4j 直查：A→B 关系边存在
  3. reindex 上传 v2 文档（供应商A → 产品C，不再提及产品B）
  4. Neo4j 直查：A→C 边存在、A→B 边已消失（旧关系被级联清理）
  5. DELETE source → Neo4j 中该来源实体消失、stats 中来源消失

说明：不用 /api/kb/search 断言——dense 搜索走 Neo4j 全文兜底路径时，
中文查询按字面匹配会返回无关实体，结果解析不稳定。图关系边才是
「对应关系是否更新」的直接证据。

前置：账号需 editor 角色（upload/reindex/delete 均要求 editor+）。

用法: .venv/Scripts/python.exe scripts/verify_kb_update.py
"""
import asyncio
import io
import subprocess
import sys

import httpx

BASE = "http://localhost:8080"
USERNAME = "claude_verify"
PASSWORD = "ClaudeVerify123!"

V1_TEXT = (
    "供应商A 与 产品B 存在对应供货关系。"
    "供应商A 是 产品B 的唯一供应商，双方自 2024 年起合作。"
)
V2_TEXT = (
    "供应商A 与 产品C 存在对应供货关系。"
    "供应商A 是 产品C 的唯一供应商，自 2025 年起合作。"
)

SOURCE = "kb-update-e2e-test.txt"

FAILED = False


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"[{'PASS' if ok else 'FAIL'}] {label} {detail}")
    if not ok:
        FAILED = True


def cypher(query: str) -> str:
    """直查 Neo4j（docker exec cypher-shell）。"""
    try:
        r = subprocess.run(
            [
                "docker", "exec", "langgraph-agent-neo4j-1",
                "cypher-shell", "-u", "neo4j", "-p", "password123",
                "--format", "plain", query,
            ],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        return (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        return f"CYPHER_ERROR: {exc}"


def connected_entities(entity: str) -> list[str]:
    """返回与实体相连的全部实体名（无向，排除自身）。"""
    out = cypher(
        f"MATCH (a)-[r]-(b) WHERE a.entity_id CONTAINS '{entity}' "
        f"RETURN b.entity_id AS target, type(r) AS rel LIMIT 50"
    )
    targets = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("target"):
            continue
        # plain 格式: "值1", "值2"（逗号+空格分隔，值带双引号）
        parts = [p.strip().strip('"') for p in line.split('", "')]
        if parts:
            targets.add(parts[0])
    return sorted(targets)


def entity_exists(entity: str) -> bool:
    out = cypher(
        f"MATCH (n) WHERE n.entity_id CONTAINS '{entity}' RETURN n.entity_id LIMIT 5"
    )
    for line in out.splitlines():
        if entity in line and "entity_id" not in line:
            return True
    return False


async def main() -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        # 登录（X-Real-IP 绕过限流）
        resp = await client.post(
            f"{BASE}/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            headers={"X-Real-IP": "10.7.7.7"},
        )
        if resp.status_code != 200:
            print(f"[FAIL] login: HTTP {resp.status_code} {resp.text[:200]}")
            sys.exit(1)
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. 上传 v1
        r = await client.post(
            f"{BASE}/api/kb/upload",
            files={"file": (SOURCE, io.BytesIO(V1_TEXT.encode("utf-8")), "text/plain")},
            headers=headers,
        )
        check("upload v1", r.status_code == 200, f"HTTP {r.status_code} {r.text[:150] if r.status_code != 200 else ''}")
        if r.status_code != 200:
            sys.exit(1)
        print(f"  v1 indexed: {r.json().get('indexed_chunks')}/{r.json().get('total_chunks')} chunks")

        # 2. Neo4j：A→B 边存在
        targets_v1 = connected_entities("供应商A")
        check("v1 graph: A->B edge", any("产品B" in t for t in targets_v1), f"connected={targets_v1}")

        # 3. reindex 上传 v2（同 source，先删旧再导新）
        r = await client.post(
            f"{BASE}/api/kb/reindex",
            files={"file": (SOURCE, io.BytesIO(V2_TEXT.encode("utf-8")), "text/plain")},
            data={"source": SOURCE},
            headers=headers,
        )
        check("reindex v2", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200] if r.status_code != 200 else ''}")
        if r.status_code == 200:
            body = r.json()
            print(f"  removed_old={body.get('removed_old_docs')}, indexed={body.get('indexed_chunks')}/{body.get('total_chunks')}")
        else:
            sys.exit(1)

        # 4. Neo4j：A→C 存在、A→B 已消失
        targets_v2 = connected_entities("供应商A")
        check("v2 graph: A->C edge", any("产品C" in t for t in targets_v2), f"connected={targets_v2}")
        check("v2 graph: A->B gone", not any("产品B" in t for t in targets_v2), f"connected={targets_v2}")

        # 5. DELETE source → 图与 stats 中均消失
        r = await client.delete(f"{BASE}/api/kb/sources/{SOURCE}", headers=headers)
        check("delete source", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}")
        print(f"  delete response: {r.text[:200]}")

        r = await client.get(f"{BASE}/api/kb/stats", headers=headers)
        sources = [s.get("source", "") for s in r.json().get("sources", [])]
        check("source gone from stats", SOURCE not in sources, f"(sources={len(sources)})")

        check("graph: entity gone after delete", not entity_exists("供应商A"))

    print("KB UPDATE TEST PASSED" if not FAILED else "[FAIL] KB UPDATE TEST FAILED")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    asyncio.run(main())
