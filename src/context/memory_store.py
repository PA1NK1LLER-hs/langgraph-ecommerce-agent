"""Mem0 记忆存储 — 封装 Mem0 客户端，提供用户记忆的 CRUD + 语义搜索。"""

import logging
from typing import Any

from mem0 import Memory

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_DIM,
    QDRANT_URL,
    MEM0_LLM_MODEL,
    MEM0_COLLECTION_NAME,
    MEM0_EMBEDDING_DIMS,
)

logger = logging.getLogger(__name__)


class UserMemoryStore:
    """Mem0 用户记忆存储。

    复用项目现有的 LLM + Embedding + Qdrant 向量库。
    """

    def __init__(self):
        self._client: Memory | None = None

    @property
    def client(self) -> Memory:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> Memory:
        # 解析 Qdrant host/port
        qdrant_url = QDRANT_URL.replace("http://", "").replace("https://", "")
        if ":" in qdrant_url:
            host, port = qdrant_url.rsplit(":", 1)
        else:
            host, port = qdrant_url, "6333"

        vector_config: dict = {
            "host": host,
            "port": int(port),
            "collection_name": MEM0_COLLECTION_NAME,
            "embedding_model_dims": MEM0_EMBEDDING_DIMS,
        }
        from config import QDRANT_API_KEY
        if QDRANT_API_KEY:
            vector_config["api_key"] = QDRANT_API_KEY

        config = {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": MEM0_LLM_MODEL,
                    "api_key": LLM_API_KEY,
                    "openai_base_url": LLM_BASE_URL,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": EMBEDDING_MODEL,
                    "api_key": EMBEDDING_API_KEY,
                    "openai_base_url": EMBEDDING_BASE_URL,
                    "embedding_dims": EMBEDDING_DIM,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": vector_config,
            },
        }

        logger.info(
            "Mem0 initialized: llm=%s, embed=%s, vector=qdrant://%s:%s/%s",
            MEM0_LLM_MODEL, EMBEDDING_MODEL,
            host, port, MEM0_COLLECTION_NAME,
        )
        return Memory.from_config(config)

    def add(
        self,
        content: str,
        *,
        user_id: str,
        category: str = "general",
        infer: bool = True,
    ) -> dict[str, Any]:
        """存入一条记忆。infer=True 时由 LLM 自动提取事实并去重合并。"""
        return self.client.add(
            content,
            user_id=user_id,
            metadata={"category": category},
            infer=infer,
        )

    def search(
        self,
        query: str,
        *,
        user_id: str,
        limit: int = 10,
        threshold: float = 0.3,
    ) -> list[dict[str, Any]]:
        """语义搜索记忆。threshold 为最低相似度分数（0-1）。"""
        results = self.client.search(
            query,
            filters={"user_id": user_id},
            top_k=limit,
            threshold=threshold,
        )
        return results.get("results", [])

    def get_all(
        self,
        *,
        user_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """列出用户全部记忆。"""
        return self.client.get_all(
            filters={"user_id": user_id}, top_k=limit
        ).get("results", [])

    def delete(self, memory_id: str) -> bool:
        """删除单条记忆。"""
        try:
            self.client.delete(memory_id)
            return True
        except Exception as exc:
            logger.warning("Failed to delete memory %s: %s", memory_id, exc)
            return False

    def delete_all(self, *, user_id: str) -> dict[str, Any]:
        """删除用户全部记忆。"""
        return self.client.delete_all(user_id=user_id)

    def search_then_delete(
        self, query: str, *, user_id: str, limit: int = 5, threshold: float = 0.4
    ) -> int:
        """语义搜索后删除匹配的记忆（仅删除分数 ≥ threshold 的），返回删除数量。"""
        results = self.search(query, user_id=user_id, limit=limit, threshold=threshold)
        deleted = 0
        for item in results:
            mem_id = item.get("id", "")
            if mem_id and self.delete(mem_id):
                deleted += 1
        return deleted


# 模块级单例
_store: UserMemoryStore | None = None


def get_memory_store() -> UserMemoryStore:
    global _store
    if _store is None:
        _store = UserMemoryStore()
    return _store
