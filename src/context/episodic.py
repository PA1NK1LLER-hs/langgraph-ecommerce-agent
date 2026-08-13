"""情景记忆（L2）— 关键事件、决策、任务完成的持久化存储。

位于三层记忆架构的中间层：
  L1: 工作记忆（当前对话窗口，~10 条消息）
  L2: 情景记忆（本模块 — 跨对话的关键事件摘要，JSON 文件存储）
  L3: 语义记忆（Mem0 向量库 — 已实现）

存储结构：
  .data/episodic/{user_id}.json → [{id, timestamp, summary, importance, tags, category, metadata}]
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认存储目录
DEFAULT_DATA_DIR = Path(".data/episodic")

# 重要性等级
IMPORTANCE_LOW = 0.3
IMPORTANCE_MEDIUM = 0.6
IMPORTANCE_HIGH = 0.9

# 单用户最大情景记忆数（超出后淘汰低重要性条目）
MAX_EPISODES_PER_USER = 100


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """返回 ISO 8601 时间戳（UTC）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_episode(
    summary: str = "",
    importance: float = IMPORTANCE_MEDIUM,
    tags: list[str] | None = None,
    category: str = "general",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12],
        "timestamp": _now_iso(),
        "summary": summary,
        "importance": importance,
        "tags": tags or [],
        "category": category,
        "metadata": metadata or {},
    }


# ---------------------------------------------------------------------------
# 存储引擎
# ---------------------------------------------------------------------------


class EpisodicStore:
    """情景记忆存储 — JSON 文件 + 内存缓存。

    每个用户一个 JSON 文件，内存中缓存完整列表。
    超出 MAX_EPISODES_PER_USER 时自动淘汰低重要性条目。
    """

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR):
        self._data_dir = Path(data_dir)
        self._cache: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _file_path(self, user_id: str) -> Path:
        # 对 user_id 做安全转义，防止路径穿越
        safe = "".join(c if c.isalnum() or c in "-_@" else "_" for c in user_id) or "default"
        return self._data_dir / f"{safe}.json"

    def _load(self, user_id: str) -> list[dict[str, Any]]:
        """从磁盘加载用户情景记忆。"""
        if user_id in self._cache:
            return self._cache[user_id]

        fp = self._file_path(user_id)
        if not fp.exists():
            self._cache[user_id] = []
            return []

        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._cache[user_id] = data
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load episodic memory for %s: %s", user_id, exc)

        self._cache[user_id] = []
        return []

    def _save(self, user_id: str) -> None:
        """将缓存写回磁盘。"""
        episodes = self._cache.get(user_id, [])
        self._data_dir.mkdir(parents=True, exist_ok=True)
        fp = self._file_path(user_id)
        fp.write_text(json.dumps(episodes, ensure_ascii=False, indent=2), encoding="utf-8")

    def _evict_if_needed(self, user_id: str) -> None:
        """超出容量上限时淘汰最低重要性的条目。"""
        episodes = self._cache.get(user_id, [])
        if len(episodes) <= MAX_EPISODES_PER_USER:
            return
        # 按 importance 升序排序，移除多余的
        episodes.sort(key=lambda e: e.get("importance", 0.0))
        removed = episodes[: len(episodes) - MAX_EPISODES_PER_USER]
        self._cache[user_id] = episodes[len(episodes) - MAX_EPISODES_PER_USER:]
        logger.info(
            "Evicted %d low-importance episodes for user %s (now %d)",
            len(removed), user_id, len(self._cache[user_id]),
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add(
        self,
        user_id: str,
        summary: str,
        *,
        importance: float = IMPORTANCE_MEDIUM,
        tags: list[str] | None = None,
        category: str = "general",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """添加一条情景记忆。

        Args:
            user_id: 用户 ID。
            summary: 事件摘要（一句话）。
            importance: 重要性 0.0-1.0（0.3 低 / 0.6 中 / 0.9 高）。
            tags: 标签列表，如 ["task", "decision"]。
            category: 分类 — task_completion / user_decision / tool_result / preference / general。
            metadata: 附加元数据（如关联的工具名、任务 ID 等）。

        Returns:
            新创建的情景记忆条目。
        """
        episode = _default_episode(summary, importance, tags, category, metadata)
        self._load(user_id)  # 确保缓存已加载
        self._cache.setdefault(user_id, []).append(episode)
        self._evict_if_needed(user_id)
        self._save(user_id)
        logger.debug("Episodic memory saved for %s: %s", user_id, summary[:80])
        return episode

    def search(
        self,
        user_id: str,
        query: str = "",
        *,
        tags: list[str] | None = None,
        category: str | None = None,
        min_importance: float = 0.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """搜索情景记忆。

        按重要性降序排列，支持关键词、标签、分类过滤。

        Args:
            user_id: 用户 ID。
            query: 关键词搜索（在 summary 和 tags 中匹配）。
            tags: 必须包含这些标签（交集匹配）。
            category: 按分类过滤。
            min_importance: 最低重要性阈值。
            limit: 返回条数上限。
        """
        episodes = self._load(user_id)

        results = []
        query_lower = query.lower() if query else ""

        for ep in episodes:
            # 重要性过滤
            if ep.get("importance", 0.0) < min_importance:
                continue

            # 分类过滤
            if category and ep.get("category") != category:
                continue

            # 标签过滤（交集 — 必须同时包含所有指定标签）
            if tags:
                ep_tags = set(ep.get("tags", []))
                if not set(tags).issubset(ep_tags):
                    continue

            # 关键词匹配
            if query_lower:
                summary = ep.get("summary", "").lower()
                ep_tags_str = " ".join(ep.get("tags", [])).lower()
                if query_lower not in summary and query_lower not in ep_tags_str:
                    # 也检查 metadata 中的字符串值
                    meta_match = False
                    for v in ep.get("metadata", {}).values():
                        if isinstance(v, str) and query_lower in v.lower():
                            meta_match = True
                            break
                    if not meta_match:
                        continue

            results.append(ep)

        # 按重要性降序
        results.sort(key=lambda e: e.get("importance", 0.0), reverse=True)
        return results[:limit]

    def get_recent(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取最近的 N 条情景记忆（按时间降序）。"""
        episodes = self._load(user_id)
        sorted_eps = sorted(episodes, key=lambda e: e.get("timestamp", ""), reverse=True)
        return sorted_eps[:limit]

    def get_high_importance(
        self,
        user_id: str,
        threshold: float = IMPORTANCE_HIGH,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取高重要性情景记忆。"""
        return self.search(user_id, min_importance=threshold, limit=limit)

    def delete(self, user_id: str, episode_id: str) -> bool:
        """删除指定 ID 的情景记忆。"""
        episodes = self._load(user_id)
        for i, ep in enumerate(episodes):
            if ep.get("id") == episode_id:
                episodes.pop(i)
                self._save(user_id)
                return True
        return False

    def delete_all(self, user_id: str) -> int:
        """删除用户全部情景记忆，返回删除数量。"""
        episodes = self._load(user_id)
        count = len(episodes)
        self._cache[user_id] = []
        self._save(user_id)
        return count

    def count(self, user_id: str) -> int:
        """用户情景记忆总数。"""
        return len(self._load(user_id))

    def clear_cache(self, user_id: str | None = None) -> None:
        """清除内存缓存（不删除磁盘文件）。"""
        if user_id:
            self._cache.pop(user_id, None)
        else:
            self._cache.clear()


# ---------------------------------------------------------------------------
# 模块单例
# ---------------------------------------------------------------------------

_store: EpisodicStore | None = None


def get_episodic_store(data_dir: Path | str = DEFAULT_DATA_DIR) -> EpisodicStore:
    global _store
    if _store is None:
        _store = EpisodicStore(data_dir=data_dir)
    return _store
