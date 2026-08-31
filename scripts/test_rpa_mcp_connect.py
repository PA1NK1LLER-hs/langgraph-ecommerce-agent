# -*- coding: utf-8 -*-
"""冒烟：RPA MCP 独立进程连接 — LLM 只见 submit_rpa_*，mcp_rpa_* 硬隐藏。

验证"RPA 永远独立进程 + 提交侧封装"在后端真实生效：
  1. 连接前 get_all_tools()：有 3 个 submit_rpa_*，无 mcp_rpa_* / 进程内 rpa_*；
  2. setup_rpa_mcp() 连接独立 RPA MCP（stdio 子进程），仍返回 3；
  3. 连接后 get_all_tools()：submit_rpa_* 仍在，mcp_rpa_* 仍被硬隐藏；
  4. _mcp_importers["RPA"] 保持连接，且 list_tools() 仍能列出 3 个原始
     rpa_* 工具（供调度器 call_tool 执行，LLM 不可见）。

运行：.venv/Scripts/python.exe scripts/test_rpa_mcp_connect.py
"""
import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _rpa_split(names: list[str]) -> tuple[list[str], list[str], list[str]]:
    """按前缀把工具名分成 (submit_rpa_*, mcp_rpa_*, 进程内 rpa_*)。"""
    submit = sorted(n for n in names if n.startswith("submit_rpa_"))
    mcp = sorted(n for n in names if n.startswith("mcp_rpa_"))
    inproc = sorted(n for n in names if n.startswith("rpa_"))
    return submit, mcp, inproc


async def main() -> None:
    from agent.mcp_setup import setup_rpa_mcp, shutdown_mcp_tools, _mcp_importers
    from agent.core import get_all_tools

    # 1. 连接前：LLM 侧契约
    before = [t.name for t in get_all_tools() if "rpa" in t.name]
    submit, mcp, inproc = _rpa_split(before)
    print("连接前 RPA 工具:", sorted(before))
    assert len(submit) == 3, f"期望 3 个 submit_rpa_*，实际 {submit}"
    assert not mcp, f"mcp_rpa_* 必须硬隐藏，实际 {mcp}"
    assert not inproc, f"不应有进程内 rpa_*，实际 {inproc}"

    # 2. 连接独立 RPA MCP（stdio 子进程）
    count = await setup_rpa_mcp()
    print("setup_rpa_mcp 返回:", count)
    assert count == 3, f"期望 3 个批量任务，实际 {count}"

    # 3. 连接后：LLM 侧契约不变
    after = [t.name for t in get_all_tools() if "rpa" in t.name]
    submit2, mcp2, inproc2 = _rpa_split(after)
    print("连接后 RPA 工具:", sorted(after))
    assert len(submit2) == 3, submit2
    assert not mcp2, f"mcp_rpa_* 必须保持隐藏，实际 {mcp2}"
    assert not inproc2, inproc2

    # 4. importer 仍持有原始 rpa_* 工具（供调度器执行，LLM 不可见）
    importer = _mcp_importers.get("RPA")
    assert importer is not None, "RPA importer 未注册"
    assert importer.connected, "RPA MCP 未连接"
    raw = await importer.list_tools()
    raw_names = sorted(t["name"] for t in raw)
    print("importer 原始工具:", raw_names)
    assert raw_names == [
        "rpa_collect_amazon_review",
        "rpa_query_campaign_spend",
        "rpa_update_track_table",
    ], raw_names

    print("OK ✅ RPA 独立进程已连接：3 个 submit_rpa_* 对 LLM 可见，3 个 mcp_rpa_* 硬隐藏")
    # 显式断开 stdio 传输：否则 asyncio.run 退出时 anyio cancel scope
    # 跨 task 关闭（Python 3.14）会让解释器退出挂起。
    await shutdown_mcp_tools()


if __name__ == "__main__":
    asyncio.run(main())
