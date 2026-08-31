# -*- coding: utf-8 -*-
"""机制冒烟：RPA 执行日志 → hub → drain 全链路（不触发任何真实业务）。

模拟 dynamic_tool_node 的真实路径：asyncio 任务里设 contextvar →
LangChain 同步 @tool 的 ainvoke（内部 run_in_executor + copy_context）→
executor 线程里经 capture_stdout_to_hub 捕获 print、WSProgressHandler 捕获
logger → 事件循环侧 drain 拿到逐条事件。

运行：.venv/Scripts/python.exe scripts/test_rpa_log_stream.py
"""
import asyncio
import logging
import sys
from pathlib import Path

# 控制台默认 gbk，测试验证打印含 emoji，先切 utf-8 避免 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.progress import (  # noqa: E402
    CURRENT_THREAD_ID,
    capture_stdout_to_hub,
    hub,
    install_rpa_log_handler,
)
from langchain_core.tools import tool  # noqa: E402

install_rpa_log_handler()


def _run(payload: dict) -> dict:
    """模拟轨迹跟踪表任务：print + logger 交替输出。"""
    print("=====打开店铺：测试店铺=====")
    logging.getLogger("skills.rpa.tasks.fake.flow").warning("下载领星FBA库存")
    print("📦 货件 FBA15XXXX 查询完成  |  匹配 12/12")
    print("所有Sheet均已完成，无需处理", flush=True)
    logging.getLogger("skills.rpa.tasks.fake.flow").info("写入Sheet [FBA]: 42 行")
    return {"status": "success", "data": payload}


@tool
def fake_rpa(store: str = "") -> dict:
    """与 adapters.py 完全一致：任务经 capture_stdout_to_hub 执行。"""
    return capture_stdout_to_hub(_run, {"store": store})


async def main() -> None:
    tid = "test-thread-stream"
    hub.subscribe(tid)
    CURRENT_THREAD_ID.set(tid)

    got: list[dict] = []

    async def consume() -> None:
        async for ev in hub.drain(tid):
            got.append(ev)
            if len(got) >= 5:
                break

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # 让 consumer 先注册 waker

    # 与 dynamic_tool_node 相同的调用路径
    result = await fake_rpa.ainvoke({"store": "测试"})

    await asyncio.wait_for(consumer, timeout=5)
    hub.unsubscribe(tid)
    CURRENT_THREAD_ID.set("")

    print("TOOL RESULT:", result)
    print("STREAMED EVENTS:", len(got))
    for ev in got:
        print(f"  [{ev.get('time')}] ({ev.get('level')}) {ev.get('content')}")

    lines = [ev["content"] for ev in got]
    assert "=====打开店铺：测试店铺=====" in lines, "print 未流式捕获"
    assert any("下载领星FBA库存" in l for l in lines), "logger 未流式捕获"
    assert any("货件 FBA15XXXX 查询完成" in l for l in lines), "print 多行捕获缺失"
    assert any("写入Sheet" in l for l in lines), "logger info 未流式捕获"
    assert result.get("status") == "success", "工具结果被破坏"
    print("OK ✅  全链路流式日志捕获通过")


if __name__ == "__main__":
    asyncio.run(main())
