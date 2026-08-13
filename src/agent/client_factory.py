"""共享 OpenAI AsyncClient 工厂。

统一管理 API Key、Base URL、认证模式（tp- 前缀等），避免在 graph.py、
summarizer.py、guard.py 中重复创建连接池。
"""

from __future__ import annotations

from openai import AsyncOpenAI
from config import LLM_API_KEY, LLM_BASE_URL


_shared_client: AsyncOpenAI | None = None


def _build_client_kwargs(api_key: str | None = None, base_url: str | None = None) -> dict:
    """构建 AsyncOpenAI 的初始化参数。

    timeout=120s / max_retries=2：上游 LLM 端点挂起时快速失败，
    避免 SDK 默认 600s 无超时把整个请求拖死（曾导致聊天"卡死"）。
    """
    key = api_key or LLM_API_KEY
    url = base_url or LLM_BASE_URL
    kwargs: dict = {
        "api_key": key,
        "base_url": url,
        "timeout": 120.0,
        "max_retries": 2,
    }
    if key.startswith("tp-"):
        kwargs["default_headers"] = {"api-key": key}
    return kwargs


def get_async_openai_client(
    api_key: str | None = None,
    base_url: str | None = None,
) -> AsyncOpenAI:
    """获取共享的 AsyncOpenAI 客户端实例（复用底层 HTTP 连接池）。

    默认使用 config 中的 LLM_API_KEY / LLM_BASE_URL。
    传入 api_key/base_url 可覆盖（如 embedding 用不同端点）。
    """
    global _shared_client
    # 仅默认配置下使用共享实例；自定义 key/url 时创建独立客户端
    if api_key is None and base_url is None:
        if _shared_client is None:
            _shared_client = AsyncOpenAI(**_build_client_kwargs())
        return _shared_client
    return AsyncOpenAI(**_build_client_kwargs(api_key, base_url))


def reset_async_client() -> None:
    """配置变更后重建共享客户端（如密钥轮换）。"""
    global _shared_client
    _shared_client = None
