"""Agent Text2Cypher 全链路速度测试。"""

import asyncio
import sys
import time
import uuid

sys.path.insert(0, "src")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def run_query(agent, query, config, label):
    from langchain_core.messages import HumanMessage

    input_state = {
        "messages": [HumanMessage(content=query)],
        "plan": "",
        "tool_failures": 0,
        "tool_retries": 0,
    }

    t_start = time.perf_counter()
    first_token = None
    total_content = ""
    model_used = "?"
    tool_calls = []

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
                extra = getattr(msg, "additional_kwargs", {}) or {}
                if extra.get("_model_used"):
                    model_used = extra["_model_used"]
        elif mode == "updates":
            for node_name, node_output in data.items():
                msgs = node_output.get("messages", [])
                for msg in msgs:
                    if msg.type == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
                        tool_calls.extend(msg.tool_calls)

    t_end = time.perf_counter()
    ttft_str = f"TTFT={first_token - t_start:.2f}s" if first_token else "TTFT=N/A"
    total = t_end - t_start
    preview = total_content.encode("utf-8", errors="replace").decode("utf-8", errors="replace")[:150]

    print(f"  [{label}] model={model_used}  {ttft_str}  总={total:.2f}s  tools={len(tool_calls)}")
    if preview:
        print(f"  [{label}] {preview}")


async def main():
    import logging
    logging.basicConfig(level=logging.WARNING)

    from agent.graph import get_agent
    from agent.mcp_setup import setup_external_mcp_tools
    from context import set_current_user

    print("=" * 60)
    print("Agent Text2Cypher 全链路测试")
    print("=" * 60)

    print("\n[0] 启动 Agent...")
    t0 = time.perf_counter()
    set_current_user("benchmark_t2c")
    await setup_external_mcp_tools()
    agent = await get_agent()
    print(f"    耗时: {(time.perf_counter() - t0):.2f}s")

    queries = [
        "德国站有哪些商品",
        "德国站自配送的商品有哪些",
        "美国站有多少个FBA商品",
        "哪个站点商品最多",
    ]

    for i, q in enumerate(queries):
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        print(f'\n[{i+1}] 查询: "{q}"')
        await run_query(agent, q, config, f"Q{i+1}")

    print(f"\n{'=' * 60}")
    print("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
