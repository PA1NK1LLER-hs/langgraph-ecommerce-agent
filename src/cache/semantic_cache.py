"""语义缓存 — 基于嵌入相似度的问答缓存。

工作原理：
1. 用户提问 → 计算 embedding → 在缓存中搜索相似问题
2. 相似度 >= 阈值 → 直接返回缓存的回答（跳过 LLM 调用）
3. 未命中 → 正常调用 LLM → 将回答存入缓存

使用 Redis 存储（需配置 REDIS_URL），无 Redis 时自动降级为内存模式。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内存缓存回退
# ---------------------------------------------------------------------------


class _MemoryCache:
    """简单的内存 LRU 缓存。"""

    def __init__(self, max_size: int = 1000):
        self._max_size = max_size
        self._store: dict[str, tuple[float, str]] = {}  # key → (expiry, value)

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expiry, value = entry
        if expiry < time.time():
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: str, ttl: int = 3600) -> None:
        # LRU eviction
        if len(self._store) >= self._max_size:
            oldest_key = next(iter(self._store))
            del self._store[oldest_key]
        self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# 语义缓存
# ---------------------------------------------------------------------------


class SemanticCache:
    """基于嵌入相似度的问答缓存。

    相似度阈值默认 0.95（极高相似才算命中，避免错误复用）。
    使用 hash(embedding_top_k_bits) 作为近似 key，避免存储完整 embedding。
    """

    def __init__(
        self,
        similarity_threshold: float = 0.95,
        default_ttl: int = 3600,
    ):
        self._threshold = similarity_threshold
        self._default_ttl = default_ttl
        self._redis = None
        self._memory = _MemoryCache(max_size=500)
        self._redis_available: bool | None = None  # None = 未检查

    async def _ensure_redis(self):
        """懒加载 Redis 连接。"""
        if self._redis_available is False:
            return None
        if self._redis is not None:
            return self._redis
        try:
            from config import REDIS_URL
            if not REDIS_URL:
                self._redis_available = False
                return None
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await self._redis.ping()
            self._redis_available = True
            logger.info("语义缓存使用 Redis: %s", REDIS_URL)
        except ImportError:
            self._redis_available = False
            logger.info("redis 包未安装，语义缓存使用内存模式")
        except Exception as e:
            self._redis_available = False
            logger.info("Redis 连接失败，语义缓存使用内存模式: %s", e)
        return self._redis

    @staticmethod
    def _hash_query(query: str) -> str:
        """计算查询的 hash key（精确匹配）。"""
        return "semcache:" + hashlib.sha256(query.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    async def _compute_embedding(text: str) -> list[float] | None:
        """计算文本的 embedding（用于语义匹配）。

        必须在当前事件循环上直接 await（embedding 客户端是共享的 AsyncOpenAI
        单例，其连接池绑定创建它的主循环）。旧实现把 asyncio.run 丢进线程池，
        再从主循环 join 等待——连接池被复用时跨循环等待，与主循环互相死锁，
        曾导致整个服务事件循环卡死（health 也超时）。超时用 wait_for 兜底。
        """
        try:
            from rag.embedding import get_embedding_func
            import asyncio
            import numpy as np
            ef = get_embedding_func()
            result = await asyncio.wait_for(ef.func([text]), timeout=10)
            if isinstance(result, np.ndarray):
                return result[0].tolist()
            return None
        except Exception:
            logger.debug("缓存 embedding 计算失败", exc_info=True)
            return None

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """余弦相似度。"""
        import math
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def get(self, query: str) -> dict[str, Any] | None:
        """查找缓存的回答。

        Returns:
            缓存内容 dict，或 None（未命中）。
        """
        # 1. 精确 hash 匹配（快速路径）
        exact_key = self._hash_query(query)
        redis = await self._ensure_redis()

        if redis:
            try:
                cached = await redis.get(exact_key)
                if cached:
                    data = json.loads(cached)
                    logger.debug("语义缓存精确命中: %s...", query[:50])
                    return data
            except Exception:
                pass

        # memory fallback
        cached = self._memory.get(exact_key)
        if cached:
            try:
                logger.debug("语义缓存精确命中（内存）: %s...", query[:50])
                return json.loads(cached)
            except Exception:
                pass

        # 2. 语义相似匹配（慢但更准）
        query_emb = await self._compute_embedding(query)
        if query_emb is None:
            return None

        # 遍历内存缓存做语义匹配（Redis 中不遍历所有 key，太昂贵）
        best_score = 0.0
        best_entry = None
        for key, (expiry, value) in list(self._memory._store.items()):
            if expiry < time.time():
                continue
            try:
                entry = json.loads(value)
                cached_emb = entry.get("_embedding")
                if cached_emb:
                    score = self._cosine_similarity(query_emb, cached_emb)
                    if score > best_score and score >= self._threshold:
                        best_score = score
                        best_entry = entry
            except Exception:
                pass

        if best_entry and best_score >= self._threshold:
            logger.debug("语义缓存相似命中 (%.3f): %s...", best_score, query[:50])
            return best_entry

        return None

    async def put(self, query: str, response: dict[str, Any], ttl: int | None = None) -> None:
        """将回答存入缓存。"""
        if ttl is None:
            ttl = self._default_ttl

        # 计算 embedding 并附加到缓存值
        emb = await self._compute_embedding(query)
        if emb:
            response["_embedding"] = emb

        value = json.dumps(response, ensure_ascii=False)
        exact_key = self._hash_query(query)

        # 写 Redis
        redis = await self._ensure_redis()
        if redis:
            try:
                await redis.setex(exact_key, ttl, value)
            except Exception:
                pass

        # 写内存
        self._memory.set(exact_key, value, ttl)

    async def clear(self) -> None:
        """清空所有缓存。"""
        self._memory.clear()
        redis = await self._ensure_redis()
        if redis:
            try:
                keys = await redis.keys("semcache:*")
                if keys:
                    await redis.delete(*keys)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 模块单例
# ---------------------------------------------------------------------------


_cache: SemanticCache | None = None


def get_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        _cache = SemanticCache()
    return _cache
