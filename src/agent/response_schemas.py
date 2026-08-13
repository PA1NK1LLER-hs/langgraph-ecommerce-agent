"""Agent 结构化输出 Schema — 供 API 调用方指定响应格式。

使用 Pydantic 定义类型安全的响应结构，支持：
- text: 自由文本（默认）
- table: 表格数据
- action_confirm: 操作确认
- task_plan: 任务计划
"""

from pydantic import BaseModel, Field
from typing import Any


class TextResponse(BaseModel):
    """默认自由文本响应。"""
    response_type: str = Field(default="text", description="响应类型标识")
    content: str = Field(..., description="Markdown 格式的回复内容")


class TableResponse(BaseModel):
    """表格数据响应。"""
    response_type: str = Field(default="table", description="响应类型标识")
    title: str = Field(..., description="表格标题")
    columns: list[str] = Field(..., description="列名列表")
    rows: list[list[Any]] = Field(..., description="数据行列表")


class ActionConfirmResponse(BaseModel):
    """操作确认响应。"""
    response_type: str = Field(default="action_confirm", description="响应类型标识")
    action: str = Field(..., description="即将执行的操作描述")
    tool_name: str = Field(..., description="工具名")
    tool_args: dict = Field(..., description="工具参数")
    risk_summary: str = Field(default="", description="风险摘要")


class TaskPlanResponse(BaseModel):
    """任务计划响应。"""
    response_type: str = Field(default="task_plan", description="响应类型标识")
    goal: str = Field(..., description="任务目标")
    steps: list[dict] = Field(..., description="执行步骤列表")
    estimated_time: str = Field(default="", description="预估耗时")


class Citation(BaseModel):
    """知识库来源引用。"""
    index: int = Field(..., description="引用编号")
    source: str = Field(..., description="来源文件名/URL")
    content_snippet: str = Field(default="", description="引用内容摘要")
    relevance_score: float = Field(default=0.0, description="相关度分数 0~1")


class CitedResponse(BaseModel):
    """带引用的回答 —— RAG 检索结果附带精确来源引用。"""
    response_type: str = Field(default="cited", description="响应类型标识")
    content: str = Field(..., description="Markdown 格式的回复内容")
    citations: list[Citation] = Field(default_factory=list, description="来源引用列表")


# 所有响应类型的 Union（引号避免前向引用问题）
AgentStructuredResponse = (
    TextResponse | TableResponse | ActionConfirmResponse
    | TaskPlanResponse | CitedResponse
)

# 按名称查找
RESPONSE_SCHEMAS: dict[str, type[BaseModel]] = {
    "text": TextResponse,
    "table": TableResponse,
    "action_confirm": ActionConfirmResponse,
    "task_plan": TaskPlanResponse,
    "cited": CitedResponse,
}
