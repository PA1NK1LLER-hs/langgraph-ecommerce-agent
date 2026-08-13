"""缓存模块 — 语义缓存 + 通用缓存工具。"""

from .semantic_cache import SemanticCache, get_cache

__all__ = ["SemanticCache", "get_cache"]
