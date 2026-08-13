"""成本追踪 — LLM API 调用 token 消耗和费用估算。

预置主流模型定价（USD/1K tokens），支持自定义模型定价配置。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模型定价表（USD / 1K tokens）
# ---------------------------------------------------------------------------


# 输入/输出价格分开计价
_MODEL_PRICES: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    # Anthropic
    "claude-sonnet-5": {"input": 0.003, "output": 0.015},
    "claude-opus-5": {"input": 0.015, "output": 0.075},
    "claude-haiku-4-5": {"input": 0.0008, "output": 0.004},
    "claude-fable-5": {"input": 0.005, "output": 0.025},
    # DeepSeek
    "deepseek-chat": {"input": 0.00014, "output": 0.00028},
    "deepseek-reasoner": {"input": 0.00055, "output": 0.00219},
    # Qwen (通义千问)
    "qwen-plus": {"input": 0.0005, "output": 0.002},
    "qwen-turbo": {"input": 0.00015, "output": 0.0006},
    "qwen-max": {"input": 0.02, "output": 0.06},
    # GLM (智谱)
    "glm-4": {"input": 0.014, "output": 0.014},
    "glm-4-flash": {"input": 0, "output": 0},  # 免费
    # Moonshot
    "moonshot-v1": {"input": 0.008, "output": 0.008},
    # 通用回退（未知模型按此估算）
    "_default": {"input": 0.001, "output": 0.005},
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """单次 LLM 调用的 token 消耗。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""

    @classmethod
    def from_response(cls, response, model: str = ""):
        """从 LLM 响应中提取 token 使用信息。"""
        usage_meta = {}
        if hasattr(response, "usage_metadata"):
            usage_meta = response.usage_metadata or {}
        elif hasattr(response, "response_metadata"):
            rm = response.response_metadata or {}
            if "token_usage" in rm:
                usage_meta = rm["token_usage"]
            else:
                usage_meta = rm

        return cls(
            prompt_tokens=usage_meta.get("input_tokens", 0),
            completion_tokens=usage_meta.get("output_tokens", 0),
            total_tokens=usage_meta.get("total_tokens", 0),
            model=model,
        )


@dataclass
class CostEstimate:
    """单次调用的费用估算。"""

    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "total_tokens": self.usage.total_tokens,
            "model": self.usage.model,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass
class SessionCosts:
    """对话会话的累计成本。"""

    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost_usd: float = 0.0
    total_llm_calls: int = 0
    total_latency_ms: float = 0.0

    def add(self, estimate: CostEstimate) -> None:
        self.total_prompt_tokens += estimate.usage.prompt_tokens
        self.total_completion_tokens += estimate.usage.completion_tokens
        self.total_cost_usd += estimate.cost_usd
        self.total_llm_calls += 1
        self.total_latency_ms += estimate.latency_ms

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "cost_usd": round(self.total_cost_usd, 6),
            "llm_calls": self.total_llm_calls,
            "latency_ms": round(self.total_latency_ms, 1),
        }


# ---------------------------------------------------------------------------
# 定价查询
# ---------------------------------------------------------------------------


def estimate_cost(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> float:
    """估算单次 LLM 调用的费用。

    Args:
        model: 模型名称。
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。

    Returns:
        USD 费用估算。
    """
    prices = None
    # 模糊匹配模型名
    model_lower = model.lower()
    for key, price in _MODEL_PRICES.items():
        if key.lower() in model_lower or model_lower in key.lower():
            prices = price
            break

    if prices is None:
        logger.debug("未知模型 %s，使用默认定价", model)
        prices = _MODEL_PRICES["_default"]

    input_cost = (prompt_tokens / 1000) * prices["input"]
    output_cost = (completion_tokens / 1000) * prices["output"]
    return input_cost + output_cost
