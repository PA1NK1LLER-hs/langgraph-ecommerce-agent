"""诊断：SUPERVISOR_PROMPT 对哪些措辞会委派给指定 specialist。

直调 supervisor 的 Flash LLM 调用（与 supervisor_node 相同参数），
打印每条候选文本 → supervisor 的原始 JSON 决策。
用法: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/diag_supervisor.py
"""
import asyncio
import json
import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from agent.specialists import SUPERVISOR_PROMPT
from agent.client_factory import get_async_openai_client
from config import LLM_FLASH_MODEL

CANDIDATES = [
    # 期望 researcher
    "请帮我做一项多步骤研究：检索知识库和我的记忆，把关于我工作偏好的信息全部找出来，整理成一份偏好档案。",
    "这是一个需要多步推理的研究任务：请用搜索工具全面调研「亚马逊广告」的投放技巧，然后按要点汇总成报告。",
    # 期望 coder
    "请完成一个多步骤编码任务：写一个 Python 脚本批量处理 Excel 订单文件，计算出各产品的销售汇总，然后运行脚本验证。",
    "请帮我做一个自动化脚本任务：编写 Python 代码从文本文件中提取邮箱地址，去重后保存到新文件，并执行验证。",
    "这是一个多工具协同的技术任务：请编写一段 Python 代码，参照知识库中的代码规范文档，并操作文件系统，完成一个数据校验脚本的编写、执行和验证。",
    # 期望 analyst
    "请完成一个多步骤数据分析任务：读取一份销售数据，统计各产品销量排名，生成柱状图，并给出洞察结论。",
]

CLASSIFIER_TEST = [
    # (label, text, 期望 specialist)
    ("researcher", "请帮我做一项多步骤研究：检索知识库和我的记忆，把关于我工作偏好的信息全部找出来，整理成一份偏好档案。"),
    ("researcher", "这是一个需要多步推理的研究任务：请用搜索工具全面调研「亚马逊广告」的投放技巧，然后按要点汇总成报告。"),
    ("coder", "请完成一个多步骤编码任务：写一个 Python 脚本批量处理 Excel 订单文件，计算出各产品的销售汇总，然后运行脚本验证。"),
    ("coder", "请帮我做一个自动化脚本任务：编写 Python 代码从文本文件中提取邮箱地址，去重后保存到新文件，并执行验证。"),
    ("coder", "这是一个多工具协同的技术任务：请编写一段 Python 代码，参照知识库中的代码规范文档，并操作文件系统，完成一个数据校验脚本的编写、执行和验证。"),
    ("coder", "请完成一个复杂的自动化任务：编写并运行一个 Python 脚本，从数据库读取订单数据，清洗后写回 Excel 文件，并输出统计结论。"),
    ("analyst", "请完成一个多步骤数据分析任务：读取一份销售数据，统计各产品销量排名，生成柱状图，并给出洞察结论。"),
]


async def decide(text: str) -> str:
    client = get_async_openai_client()
    resp = await client.chat.completions.create(
        model=LLM_FLASH_MODEL,
        messages=[
            {"role": "system", "content": SUPERVISOR_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or "{}"


async def classify(text: str) -> tuple[str, bool]:
    """复刻 classify_intent 的 Flash 调用（不含工具分组/校验），返回 (intent, needs_rag)。"""
    from agent.graph import CLASSIFIER_SYSTEM, CLASSIFIER_USER, _build_tool_catalog
    client = get_async_openai_client()
    prompt = CLASSIFIER_SYSTEM + "\n\n" + CLASSIFIER_USER.format(
        tool_catalog=_build_tool_catalog(),
        user_query=text,
    )
    resp = await client.chat.completions.create(
        model=LLM_FLASH_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
        return parsed.get("intent", "?"), bool(parsed.get("needs_rag", False))
    except json.JSONDecodeError:
        return "INVALID_JSON", False


async def main() -> None:
    print("═══ Supervisor 委派 ═══")
    for t in CANDIDATES:
        try:
            out = await decide(t)
            try:
                parsed = json.loads(out)
                sp = parsed.get("specialist", "?")
            except json.JSONDecodeError:
                sp = "INVALID_JSON"
            print(f"[{sp:9s}] {t[:40]}...  ->  {out[:150]}")
        except Exception as exc:
            print(f"[ERROR   ] {t[:40]}...  ->  {type(exc).__name__}: {exc}")

    print("\n═══ 分类器意图（需 complex 才会进 supervisor）═══")
    for label, text in CLASSIFIER_TEST:
        try:
            intent, rag = await classify(text)
            ok = "✓" if intent == "complex" else "✗"
            print(f"[{label:10s}] intent={intent:10s} needs_rag={rag} {ok}  {text[:30]}...")
        except Exception as exc:
            print(f"[{label:10s}] ERROR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
