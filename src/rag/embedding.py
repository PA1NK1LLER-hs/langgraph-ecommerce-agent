"""Embedding 函数 — OpenAI 兼容端点，异步分批处理批量请求。"""

import numpy as np
from openai import AsyncOpenAI

from config import EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIM

BATCH_SIZE = 10

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    """获取复用的 AsyncOpenAI 客户端实例（懒加载单例）。

    显式超时：上游 embedding 端点无响应时快速失败，避免无超时的
    默认配置（openai SDK 默认 600s）把调用方拖死。
    """
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_BASE_URL,
            timeout=30.0,
            max_retries=1,
        )
    return _client


async def _embed(texts: list[str]) -> np.ndarray:
    """调用 embedding 模型，分批处理批量请求（真正异步，复用连接池）。"""
    if not texts:
        return np.array([])

    client = _get_client()
    all_embeddings = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
        all_embeddings.extend([item.embedding for item in resp.data])

    return np.array(all_embeddings)


_ef: "EmbeddingFunc | None" = None


def get_embedding_func() -> "EmbeddingFunc":
    """获取 LightRAG EmbeddingFunc 实例（单例）。"""
    global _ef
    if _ef is None:
        from lightrag.base import EmbeddingFunc

        _ef = EmbeddingFunc(
            embedding_dim=EMBEDDING_DIM,
            max_token_size=8192,
            model_name=EMBEDDING_MODEL,
            func=_embed,
        )
    return _ef
