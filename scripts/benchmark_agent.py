"""Agent 启动 & 查询速度测试 — DeepSeek 环境。"""

import asyncio
import logging
import sys
import time
import uuid

# Windows GBK 编码无法输出 emoji
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, "src")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main():
    from langchain_core.messages import HumanMessage

    print("=" * 60)
    print("Agent 启动 + 查询速度测试")
    print("=" * 60)

    # ── 1. 启动计时 ──
    print("\n[1] 启动中...")
    t0 = time.perf_counter()

    from agent.graph import get_agent
    from agent.mcp_setup import setup_external_mcp_tools
    from context import (
        set_current_user,
        load_user_context,
        format_context_summary,
    )

    set_current_user("benchmark_test")

    t1 = time.perf_counter()
    print(f"    导入模块: {(t1 - t0):.2f}s")

    # MCP
    await setup_external_mcp_tools()
    t2 = time.perf_counter()
    print(f"    MCP 连接:  {(t2 - t1):.2f}s")

    # Agent 图构建
    agent = await get_agent()
    t3 = time.perf_counter()
    print(f"    Agent 构建: {(t3 - t2):.2f}s")
    print(f"    启动总耗时: {(t3 - t0):.2f}s")

    # ── 2. 查询测试 ──
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    queries = [
        "你好，请用一句话介绍你自己",
        "1+1等于几？",
        "今天是星期几？",
    ]

    for i, q in enumerate(queries):
        print(f"\n[2.{i+1}] 查询: \"{q}\"")

        input_state = {
            "messages": [HumanMessage(content=q)],
            "plan": "",
            "tool_failures": 0,
            "tool_retries": 0,
        }

        t_start = time.perf_counter()
        first_token = None
        total_content = ""

        try:
            async for chunk in agent.astream(
                input_state, config=config, stream_mode=["updates", "messages"]
            ):
                mode, data = chunk
                if mode == "messages":
                    msg, _meta = data
                    if msg.type == "ai" and msg.content:
                        if first_token is None:
                            first_token = time.perf_counter()
                        total_content += str(msg.content)
                elif mode == "updates":
                    for node_name, node_output in data.items():
                        msgs = node_output.get("messages", [])
                        for msg in msgs:
                            if msg.type == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    print(f"    [调用] {tc['name']}")
                            elif msg.type == "tool":
                                name = getattr(msg, "name", "?")
                                result_text = str(msg.content)[:150]
                                is_error = (
                                    isinstance(msg.content, dict)
                                    and msg.content.get("status") == "error"
                                )
                                prefix = "ERROR" if is_error else "OK"
                                print(f"    [{prefix}] {name}: {result_text}")
                            elif msg.type == "ai" and msg.content and not total_content:
                                # updates mode also delivers AI content
                                if first_token is None:
                                    first_token = time.perf_counter()
                                total_content += str(msg.content)

            t_end = time.perf_counter()
            ttft = (first_token - t_start) if first_token else None
            total_time = t_end - t_start
            ttft_str = f"TTFT: {ttft:.2f}s" if ttft else "TTFT: N/A"
            safe_content = total_content.encode("utf-8", errors="replace").decode("utf-8", errors="replace")[:80]
            print(f"    {ttft_str}  总: {total_time:.2f}s  回答: {safe_content}")

        except Exception as exc:
            print(f"    FAIL: {exc}")

    print(f"\n{'=' * 60}")
    print("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
