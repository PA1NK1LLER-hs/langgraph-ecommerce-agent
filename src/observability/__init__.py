"""Langfuse 可观测性集成 — Callback Handler 自动注入。

使用方式：
1. 设置环境变量 LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY
2. 在 call_model 中传入 callbacks=[get_langfuse_handler()]
3. 所有 LLM 调用自动上报到 Langfuse Dashboard
"""

import os
import logging

logger = logging.getLogger(__name__)

_langfuse_handler = None


def get_langfuse_handler():
    """获取 Langfuse Callback Handler（如果已配置）。

    延迟初始化：首次调用时检查环境变量，创建 handler 并缓存。
    返回 None 表示未配置 Langfuse（无追踪但不影响功能）。

    Returns:
        CallbackHandler | None
    """
    global _langfuse_handler
    if _langfuse_handler is not None:
        return _langfuse_handler

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        logger.debug("Langfuse not configured — tracing disabled (set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY)")
        return None

    try:
        from langfuse.langchain import CallbackHandler

        _langfuse_handler = CallbackHandler(
            session_id=os.getenv("LANGFUSE_SESSION_ID", "langgraph-agent"),
            user_id=os.getenv("LANGFUSE_USER_ID", "default"),
        )
        logger.info("Langfuse tracing enabled (session: %s)", _langfuse_handler.session_id)
        return _langfuse_handler
    except ImportError:
        logger.warning("langfuse package not installed — tracing disabled")
        return None
    except Exception as exc:
        logger.warning("Langfuse initialization failed: %s", exc)
        return None


def get_callbacks():
    """获取当前应使用的回调处理器列表。

    返回非空列表时，call_model 应将其传给 LLM 的 ainvoke。

    Returns:
        list[CallbackHandler] | None
    """
    handler = get_langfuse_handler()
    if handler is not None:
        return [handler]
    return None
