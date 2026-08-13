"""入口模块 — LangGraph 通用 AI 助手交互式命令行。"""

import asyncio
import logging
import uuid
import warnings

from langchain_core.messages import HumanMessage

from context import (
    set_current_user,
    get_current_user,
    load_user_context,
    format_context_summary,
    add_memory,
    forget_memory,
)
from agent.graph import get_agent, rebuild_agent
from agent.mcp_setup import setup_external_mcp_tools, shutdown_mcp_tools
from agent.utils import build_memory_injection, truncate_tool_result
from skills import list_skills


warnings.filterwarnings("ignore", category=DeprecationWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# LightRAG 内部 WARNING 大多是空集合初始化 / Neo4j Community 版限制等无害噪音
logging.getLogger("lightrag").setLevel(logging.ERROR)
logging.getLogger("Qdrant").setLevel(logging.ERROR)
logging.getLogger("neo4j").setLevel(logging.ERROR)
logger = logging.getLogger("agent")


async def main() -> None:
    import os
    import sys

    # Windows 控制台默认 GBK 编码，无法输出 emoji；切换为 UTF-8（容错替换）
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 启动时检查必需环境变量
    from config import LLM_API_KEY
    _missing: list[str] = []
    if not LLM_API_KEY:
        _missing.append("LLM_API_KEY")
    if not os.getenv("EMBEDDING_API_KEY"):
        _missing.append("EMBEDDING_API_KEY")
    if _missing:
        print(f"ERROR: 缺少必需的环境变量: {', '.join(_missing)}")
        print("请在 .env 文件中设置后重试。")
        sys.exit(1)

    _warn_vars = {
        "ZINIAO_PASSWORD": "紫鸟浏览器 RPA 登录密码",
        "CAMPAIGN_SPEND_EXCEL_PATH": "广告花费 Excel 文件路径",
    }
    for _var, _desc in _warn_vars.items():
        if not os.getenv(_var):
            print(f"WARNING: {_var} 未设置 — {_desc}")

    default_user = os.getenv("AGENT_USER_ID", "default_user")
    set_current_user(default_user)

    print("=" * 60)
    print("LangGraph 通用 AI 助手 — Flash/Pro 双模型架构")

    user_ctx = load_user_context(get_current_user())
    ctx_summary = format_context_summary(user_ctx)
    print(f"用户: {get_current_user()}  |  {ctx_summary}")


    # 将已有记忆注入 System Prompt（复用 agent/utils.py 中的共享函数）
    context_injection = build_memory_injection(user_ctx.get("memories", []))

    # 列出技能供启动时确认
    skills = list_skills()
    skill_names = [s["name"] for s in skills]
    print(f"  已加载技能: {skill_names}")

    print("=" * 60)
    print("  命令: /help /skills /context /remember /forget /user /exit /kb")
    print()

    print("[MCP] Connecting external MCP servers...")
    await setup_external_mcp_tools()

    agent = await get_agent(context_summary=context_injection)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    while True:
        try:
            user_input = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if not user_input:
            continue

        if user_input.lower() in ("/exit", "/quit"):
            print("再见。")
            break

        if user_input.lower() == "/help":
            print("命令列表：")
            print("  /help          显示帮助")
            print("  /skills        列出已注册技能")
            print("  /context       查看当前用户记忆")
            print("  /remember <内容> 存入一条记忆")
            print("  /forget <描述>  删除匹配的记忆")
            print("  /user <name>   切换用户")
            print("  /who           查看当前用户")
            print("  /logs          查看最近代码日志")
            print("  /kb            查看知识库统计")
            print("  /reset         开启新对话线程")
            print("  /exit          退出")
            print()
            continue

        if user_input.lower() == "/skills":
            skills_list = list_skills()
            if skills_list:
                for s in skills_list:
                    print(f"  {s['name']}: {s['description'][:120]}")
            else:
                print("  (暂无已注册技能)")
            print()
            continue

        if user_input.lower() == "/logs":
            from skills.code_executor import list_code_logs
            logs = list_code_logs(20)
            if logs:
                print(f"  最近 {len(logs)} 个代码日志:")
                for log in logs:
                    print(f"  {log['name']} ({log['size']}B) {log['modified']}")
            else:
                print("  (暂无代码日志)")
            print()
            continue

        if user_input.lower() == "/kb":
            from rag import list_sources
            from rag.indexer import get_indexer
            idx = get_indexer()
            sources = list_sources()
            print(f"  知识库: {idx.count()} 个分块, {len(sources.get('sources', []))} 个来源")
            for src in sources.get("sources", []):
                print(f"  [{src['chunks']} chunks] {src['source']}")
            print()
            continue

        if user_input.lower() == "/context":
            ctx = load_user_context(get_current_user())
            memories = ctx.get("memories", [])
            if memories:
                print(f"  共 {len(memories)} 条记忆:")
                for i, m in enumerate(memories, 1):
                    text = m.get("memory", "")
                    cat = m.get("metadata", {}).get("category", "general")
                    mem_id = str(m.get("id", ""))[:12]
                    if text:
                        print(f"  [{i}] [{cat}] {text[:120]} ({mem_id})")
            else:
                print("  (暂无记忆)")
            print()
            continue

        if user_input.lower().startswith("/remember "):
            content = user_input[10:].strip()
            if content:
                r = add_memory(get_current_user(), content=content)
                print(f"  已存入记忆: {content[:80]}...\n")
            else:
                print("  用法: /remember <要记住的自然语言描述>\n")
            continue

        if user_input.lower().startswith("/forget "):
            query = user_input[8:].strip()
            if query:
                r = forget_memory(get_current_user(), query=query)
                deleted = r.get("deleted", 0)
                print(f"  已删除 {deleted} 条匹配的记忆\n")
            else:
                print("  用法: /forget <要删除的记忆描述>\n")
            continue

        if user_input.lower() == "/who":
            print(f"  当前用户: {get_current_user()}\n")
            continue

        if user_input.lower().startswith("/user "):
            new_user = user_input[6:].strip()
            if new_user:
                set_current_user(new_user)
                user_ctx = load_user_context(new_user)
                summary = format_context_summary(user_ctx)
                context_injection = build_memory_injection(user_ctx.get("memories", []))
                await rebuild_agent(context_summary=context_injection)
                config = {"configurable": {"thread_id": str(uuid.uuid4())}}
                print(f"  已切换到: {new_user}（已开启新对话）  |  {summary}\n")
            else:
                print("  用法: /user <用户名>\n")
            continue

        if user_input.lower() == "/reset":
            config = {"configurable": {"thread_id": str(uuid.uuid4())}}
            print("  已开启新对话线程。\n")
            continue

        # 调用 Agent（流式输出）
        print()

        stream_config = config.copy()

        input_state = {
            "messages": [HumanMessage(content=user_input)],
            "plan": "",
            "tool_failures": 0,
            "tool_retries": 0,
            "rag_context": "",
        }

        try:
            in_ai_response = False
            async for chunk in agent.astream(
                input_state,
                config=stream_config,
                stream_mode=["updates", "messages"],
            ):
                mode, data = chunk

                # ── Token 级流式输出 ──
                if mode == "messages":
                    msg, _metadata = data
                    if msg.type == "ai" and msg.content:
                        if not in_ai_response:
                            in_ai_response = True
                            model_used = getattr(msg, "additional_kwargs", {}).get("_model_used", "")
                            if model_used:
                                print(f"[{model_used}] ", end="", flush=True)
                        print(msg.content, end="", flush=True)
                    continue

                # ── 节点级更新（保持原有逻辑）──
                if in_ai_response:
                    print()
                    in_ai_response = False

                for node_name, node_output in data.items():
                    # 规划节点输出
                    if node_name == "planner" and node_output.get("plan"):
                        plan_text = node_output["plan"]
                        print("  [计划] 已为复杂任务生成执行计划:")
                        for line in plan_text.split("\n"):
                            if line.strip():
                                print(f"          {line}")
                        print()
                        continue

                    # 反思节点输出
                    if node_name == "reflect":
                        msgs = node_output.get("messages", [])
                        for msg in msgs:
                            if msg.type == "ai" and msg.content:
                                print("  [反思] 上次工具调用出错，正在重试...")
                        continue

                    # 失败追踪节点（静默）
                    if node_name == "track_failures":
                        continue

                    msgs = node_output.get("messages", [])
                    for msg in msgs:
                        if msg.type == "ai":
                            ak = getattr(msg, "additional_kwargs", {}) or {}
                            node = ak.get("_node", "")
                            model_used = ak.get("_model_used", "")
                            model_label = f"[{model_used}] " if model_used else ""
                            if node == "reflect":
                                continue
                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    args_brief = str(tc.get("args", {}))[:120]
                                    print(f"  {model_label}[调用] {tc['name']}({args_brief})")
                            if msg.content:
                                print(f"{model_label}Agent > {msg.content}")
                            # 内置联网搜索来源
                            annotations = ak.get("annotations")
                            if annotations:
                                sources = [a for a in annotations if a.get("type") == "url_citation"]
                                if sources:
                                    print(f"  [来源] 共 {len(sources)} 条搜索结果:")
                                    for s in sources[:5]:
                                        print(f"         {s.get('title', '?')[:60]}")
                                        print(f"         {s.get('url', '')}")
                                    if len(sources) > 5:
                                        print(f"         ... 还有 {len(sources) - 5} 条")

                        elif msg.type == "tool":
                            result_text = truncate_tool_result(msg.content)
                            name = getattr(msg, "name", "?")
                            is_error = (
                                isinstance(msg.content, dict) and msg.content.get("status") == "error"
                                or isinstance(msg.content, str)
                                and ('"status": "error"' in msg.content
                                     or '"status":"error"' in msg.content)
                            )
                            prefix = "  [错误]" if is_error else "  [结果]"
                            print(f"{prefix} {name}: {result_text}")

            if in_ai_response:
                print()
            print()

        except Exception as exc:
            logger.exception("Agent 执行异常")
            error_msg = str(exc)
            if len(error_msg) > 200:
                error_msg = error_msg[:200] + "..."
            print(f"Agent 执行异常: {error_msg}")
            print("详细信息已记录到日志。提示: 可以尝试 /reset 开启新对话线程后重试。\n")




    await shutdown_mcp_tools()


if __name__ == "__main__":
    asyncio.run(main())
