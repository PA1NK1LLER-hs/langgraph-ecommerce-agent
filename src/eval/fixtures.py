"""回归测试 Fixtures — 确保 prompt 改动不破坏已有能力。

每个用例定义了用户问题、最低评分阈值和预期的意图/工具。
运行 scripts/run_eval.py 时自动加载并评估。
"""

from typing import TypedDict


class EvalCase(TypedDict, total=False):
    """评估用例定义。"""
    name: str
    question: str
    min_relevance: int
    min_accuracy: int
    min_completeness: int
    min_tool_usage: int
    min_overall: int
    expect_intent: str
    expect_tools: list[str]


REGRESSION_TESTS: list[EvalCase] = [
    # ── 基础对话 ──
    {
        "name": "greeting",
        "question": "你好",
        "min_relevance": 4,
        "min_overall": 4,
        "expect_intent": "trivial",
    },
    {
        "name": "farewell",
        "question": "再见，谢谢你的帮助",
        "min_relevance": 4,
        "min_overall": 4,
        "expect_intent": "trivial",
    },

    # ── 知识检索 ──
    {
        "name": "knowledge_search",
        "question": "查询产品 SKU-001 的库存信息",
        "min_relevance": 3,
        "min_tool_usage": 3,
        "expect_intent": "knowledge",
        "expect_tools": ["tool_search_knowledge"],
    },

    # ── 记忆保存 ──
    {
        "name": "memory_save",
        "question": "我叫张三，在ABC公司做运营",
        "min_relevance": 4,
        "min_tool_usage": 3,
        "expect_tools": ["tool_add_memory"],
    },

    # ── 文件操作 ──
    {
        "name": "file_list",
        "question": "列出当前目录下的所有文件",
        "min_relevance": 4,
        "min_tool_usage": 3,
        "expect_intent": "file",
    },

    # ── 时间查询 ──
    {
        "name": "time_query",
        "question": "今天是几月几号？星期几？",
        "min_relevance": 4,
        "min_overall": 4,
        "expect_intent": "time",
    },

    # ── 代码执行 ──
    {
        "name": "code_exec_simple",
        "question": "用Python计算1到100的所有质数之和",
        "min_relevance": 3,
        "min_tool_usage": 3,
        "expect_intent": "code",
    },

    # ── 知识整合（RAG + 知识库）──
    {
        "name": "knowledge_query",
        "question": "帮我查一下知识库中关于产品定价的策略",
        "min_relevance": 3,
        "expect_intent": "knowledge",
        "expect_tools": ["tool_search_knowledge"],
    },
]
