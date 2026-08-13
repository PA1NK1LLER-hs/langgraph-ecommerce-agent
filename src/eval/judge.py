"""LLM-as-Judge 评估器。

使用独立的 Judge LLM 对 Agent 的响应进行多维度评分。
评分维度：相关性、准确性、完整性、工具使用、综合评分。
"""

from pydantic import BaseModel, Field


class EvalScores(BaseModel):
    """多维度评估得分（1-5 分制）。"""
    relevance: int = Field(ge=1, le=5, description="相关性：回答是否切题")
    accuracy: int = Field(ge=1, le=5, description="准确性：事实是否正确")
    completeness: int = Field(ge=1, le=5, description="完整性：是否遗漏关键信息")
    tool_usage: int = Field(ge=1, le=5, description="工具使用：是否选择了正确的工具")
    overall: int = Field(ge=1, le=5, description="综合评分")
    notes: str = Field(default="", description="评价备注")


JUDGE_PROMPT = """你是一个 Agent 评估专家。对以下 Agent 响应进行多维度评分。

## 用户问题
{question}

## Agent 响应
{answer}

## 工具调用记录
{tool_calls}

## 评分标准
- relevance (1-5): 回答是否准确回应了用户问题
  5=精准切题，4=基本切题，3=部分相关，2=偏题，1=完全无关
- accuracy (1-5): 回答中的事实是否正确
  5=全部正确，4=基本正确有小瑕疵，3=部分错误，2=多数错误，1=完全错误
- completeness (1-5): 是否遗漏了关键信息
  5=信息完整，4=基本完整，3=有遗漏，2=遗漏较多，1=关键信息缺失
- tool_usage (1-5): 工具选择和使用是否恰当
  5=工具选择精准高效，4=工具选择正确，3=部分工具选择欠佳，
  2=工具使用有明显问题，1=工具选择完全错误或遗漏必要的工具
- overall (1-5): 综合评分

请给出评分和简短备注。"""


async def judge_response(
    question: str,
    answer: str,
    tool_calls: str,
    judge_llm,
) -> EvalScores:
    """评估单次 Agent 响应。

    Args:
        question: 用户原始问题
        answer: Agent 最终文本响应
        tool_calls: 工具调用记录摘要
        judge_llm: 用于评估的 LLM 实例（建议使用独立的廉价模型）

    Returns:
        EvalScores: 多维度评分结果
    """
    from langchain_core.messages import SystemMessage

    prompt = JUDGE_PROMPT.format(
        question=question,
        answer=answer,
        tool_calls=tool_calls,
    )
    response = await judge_llm.with_structured_output(EvalScores).ainvoke([
        SystemMessage(content=prompt),
    ])
    return response
