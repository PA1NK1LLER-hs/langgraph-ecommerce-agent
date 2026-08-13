"""情景记忆（L2）测试 — EpisodicStore + Manager API。"""

import json
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

# ── 在 import context 之前 mock mem0（避免 ModuleNotFoundError）──
if "mem0" not in sys.modules:
    sys.modules["mem0"] = MagicMock()


# ═══════════════════════════════════════════════════
# EpisodicStore 单元测试
# ═══════════════════════════════════════════════════


class TestEpisodicStore:
    """EpisodicStore 核心 CRUD 测试。"""

    @pytest.fixture
    def store(self):
        from context.episodic import EpisodicStore
        with tempfile.TemporaryDirectory() as tmpdir:
            s = EpisodicStore(data_dir=tmpdir)
            yield s
            s.clear_cache()

    def test_add_and_retrieve(self, store):
        store.add("user1", "完成了 Kubernetes 集群部署", importance=0.9,
                  tags=["deployment", "k8s"], category="task_completion")

        results = store.search("user1", query="")
        assert len(results) == 1
        assert results[0]["summary"] == "完成了 Kubernetes 集群部署"
        assert results[0]["importance"] == 0.9
        assert "deployment" in results[0]["tags"]
        assert results[0]["category"] == "task_completion"

    def test_add_multiple_episodes(self, store):
        store.add("user1", "决策：使用 Redis 作为缓存", importance=0.9,
                  tags=["decision"], category="user_decision")
        store.add("user1", "偏好：简洁的回复风格", importance=0.5,
                  tags=["preference"], category="preference")
        store.add("user1", "完成了数据清洗脚本", importance=0.6,
                  tags=["task"], category="task_completion")

        assert store.count("user1") == 3

    def test_search_by_keyword(self, store):
        store.add("user1", "完成了 Kubernetes 集群部署", importance=0.9,
                  tags=["deployment"], category="task_completion")
        store.add("user1", "偏好：使用 Python 做数据分析", importance=0.5,
                  tags=["preference"], category="preference")
        store.add("user1", "决策：迁移到 AWS EKS", importance=0.8,
                  tags=["decision"], category="user_decision")

        # 关键词匹配 summary
        results = store.search("user1", query="Kubernetes")
        assert len(results) == 1
        assert "Kubernetes" in results[0]["summary"]

        # 关键词不匹配应返回空
        results = store.search("user1", query="Docker")
        assert len(results) == 0

        # 空查询返回全部
        results = store.search("user1", query="")
        assert len(results) == 3

    def test_search_by_category(self, store):
        store.add("user1", "完成部署", importance=0.9, category="task_completion")
        store.add("user1", "选择 React", importance=0.7, category="user_decision")
        store.add("user1", "偏好短回复", importance=0.4, category="preference")

        results = store.search("user1", category="task_completion")
        assert len(results) == 1
        assert results[0]["category"] == "task_completion"

    def test_search_by_tags(self, store):
        store.add("user1", "部署到生产环境", importance=0.9,
                  tags=["deployment", "production"], category="task_completion")
        store.add("user1", "部署到测试环境", importance=0.6,
                  tags=["deployment", "staging"], category="task_completion")
        store.add("user1", "代码审查", importance=0.5,
                  tags=["code", "review"], category="general")

        # 单标签匹配
        results = store.search("user1", tags=["production"])
        assert len(results) == 1
        assert "生产环境" in results[0]["summary"]

        # 多标签交集匹配
        results = store.search("user1", tags=["deployment", "staging"])
        assert len(results) == 1
        assert "测试环境" in results[0]["summary"]

        # 只匹配一个标签不够
        results = store.search("user1", tags=["deployment", "production", "nonexistent"])
        assert len(results) == 0

    def test_search_by_min_importance(self, store):
        store.add("user1", "high", importance=0.95)
        store.add("user1", "medium", importance=0.6)
        store.add("user1", "low", importance=0.2)

        results = store.search("user1", min_importance=0.7)
        assert len(results) == 1
        assert results[0]["summary"] == "high"

        results = store.search("user1", min_importance=0.5)
        assert len(results) == 2

    def test_results_sorted_by_importance(self, store):
        store.add("user1", "low", importance=0.2)
        store.add("user1", "high", importance=0.95)
        store.add("user1", "medium", importance=0.6)

        results = store.search("user1", query="")
        assert results[0]["importance"] == 0.95
        assert results[1]["importance"] == 0.6
        assert results[2]["importance"] == 0.2

    def test_get_recent(self, store):
        # 按顺序添加，间隔 1.1s 以产生不同时间戳（ISO 格式精确到秒）
        import time
        store.add("user1", "event 1", importance=0.5)
        time.sleep(1.1)
        store.add("user1", "event 2", importance=0.5)
        time.sleep(1.1)
        store.add("user1", "event 3", importance=0.5)

        recent = store.get_recent("user1", limit=2)
        assert len(recent) == 2
        # 按时间降序：最新添加的在最前
        assert recent[0]["summary"] == "event 3"
        assert recent[1]["summary"] == "event 2"

    def test_get_high_importance(self, store):
        store.add("user1", "critical", importance=0.95)
        store.add("user1", "important", importance=0.8)
        store.add("user1", "normal", importance=0.5)

        high = store.get_high_importance("user1", threshold=0.8)
        assert len(high) >= 2

    def test_delete(self, store):
        ep = store.add("user1", "将被删除的记忆")
        ep_id = ep["id"]

        assert store.delete("user1", ep_id) is True
        assert store.count("user1") == 0
        # 重复删除应返回 False
        assert store.delete("user1", ep_id) is False

    def test_delete_all(self, store):
        store.add("user1", "记忆 1")
        store.add("user1", "记忆 2")
        store.add("user1", "记忆 3")

        count = store.delete_all("user1")
        assert count == 3
        assert store.count("user1") == 0

    def test_persistence_across_instances(self, store):
        """不同实例共享同一数据目录，数据应持久化。"""
        from context.episodic import EpisodicStore

        ep = store.add("user1", "持久化测试", importance=0.7)
        ep_id = ep["id"]

        # 创建新实例指向同一目录
        store2 = EpisodicStore(data_dir=store._data_dir)
        try:
            results = store2.search("user1", query="持久化测试")
            assert len(results) == 1
            assert results[0]["id"] == ep_id
        finally:
            store2.clear_cache()

    def test_user_isolation(self, store):
        store.add("user1", "用户1的记忆")
        store.add("user2", "用户2的记忆")

        r1 = store.search("user1", query="")
        r2 = store.search("user2", query="")
        assert len(r1) == 1
        assert len(r2) == 1
        assert r1[0]["summary"] == "用户1的记忆"
        assert r2[0]["summary"] == "用户2的记忆"

    def test_summary_in_metadata_search(self, store):
        """metadata 中的字符串值也应参与搜索。"""
        store.add("user1", "代码部署",
                  metadata={"tool": "kubectl", "cluster": "prod-eu-west"})

        # 按 metadata 字符串搜索
        results = store.search("user1", query="kubectl")
        assert len(results) == 1

        results = store.search("user1", query="prod-eu-west")
        assert len(results) == 1

    def test_limit_parameter(self, store):
        for i in range(10):
            store.add("user1", f"事件 {i}", importance=0.5)

        results = store.search("user1", query="", limit=5)
        assert len(results) == 5

    def test_eviction_on_max_capacity(self, store):
        """超出 MAX_EPISODES_PER_USER 时自动淘汰低重要性条目。"""
        from context.episodic import MAX_EPISODES_PER_USER

        # 添加超出上限的条目
        total = MAX_EPISODES_PER_USER + 10
        for i in range(total):
            # 前5条低重要性，其余的随机
            imp = 0.1 if i < 5 else 0.5 + (i % 10) * 0.02
            store.add("user1", f"事件 {i}", importance=imp)

        assert store.count("user1") <= MAX_EPISODES_PER_USER
        # 低重要性的应该已被淘汰
        results = store.search("user1", query="事件 0")
        assert len(results) == 0  # 事件 0 (importance=0.1) 应被淘汰

    def test_default_values(self, store):
        ep = store.add("user1", "默认值测试")
        assert ep["importance"] == 0.6  # 默认
        assert ep["tags"] == []
        assert ep["category"] == "general"
        assert ep["metadata"] == {}
        assert "id" in ep
        assert "timestamp" in ep

    def test_safe_user_id(self, store):
        """特殊字符的 user_id 应被安全转义。"""
        store.add("../etc/passwd", "危险路径测试")
        # 不应创建目录穿越文件
        fp = store._file_path("../etc/passwd")
        assert ".." not in str(fp.name)  # 文件名中不应含 ..
        assert fp.parent == store._data_dir


# ═══════════════════════════════════════════════════
# Manager API 集成测试
# ═══════════════════════════════════════════════════


class TestEpisodicManager:
    """Manager API 层测试 — 验证参数透传和错误处理。"""

    @pytest.fixture
    def clean_store(self, monkeypatch):
        import context.episodic as emod
        with tempfile.TemporaryDirectory() as tmpdir:
            store = emod.EpisodicStore(data_dir=tmpdir)
            monkeypatch.setattr(emod, "_store", store)
            yield store
            store.clear_cache()

    def test_save_episodic_memory_success(self, clean_store):
        from context.manager import save_episodic_memory
        result = save_episodic_memory(
            "user1",
            "用户决定使用 PostgreSQL 作为主数据库",
            importance=0.9,
            tags=["decision", "database"],
            category="user_decision",
            metadata={"context": "技术选型会议"},
        )
        assert result["status"] == "success"
        assert result["episode"]["summary"] == "用户决定使用 PostgreSQL 作为主数据库"
        assert result["episode"]["importance"] == 0.9
        assert "decision" in result["episode"]["tags"]

    def test_search_episodic_memory(self, clean_store):
        from context.manager import save_episodic_memory, search_episodic_memory

        save_episodic_memory("user1", "部署到 K8s 生产环境", importance=0.9, tags=["deployment"])
        save_episodic_memory("user1", "代码审查通过", importance=0.5, tags=["code"])

        results = search_episodic_memory("user1", query="K8s")
        assert results["status"] == "success"
        assert results["count"] == 1

        results = search_episodic_memory("user1", tags=["deployment"])
        assert results["count"] == 1

    def test_get_recent_episodes(self, clean_store):
        from context.manager import save_episodic_memory, get_recent_episodes

        import time
        save_episodic_memory("user1", "最旧事件", importance=0.5)
        time.sleep(1.1)
        save_episodic_memory("user1", "中间事件", importance=0.5)
        time.sleep(1.1)
        save_episodic_memory("user1", "最新事件", importance=0.5)

        result = get_recent_episodes("user1", limit=2)
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["results"][0]["summary"] == "最新事件"

    def test_delete_episodic_memory(self, clean_store):
        from context.manager import save_episodic_memory, delete_episodic_memory

        ep = save_episodic_memory("user1", "待删除事件")["episode"]
        result = delete_episodic_memory("user1", ep["id"])
        assert result["status"] == "success"
        assert result["deleted"] is True

    def test_get_conversation_context(self, clean_store):
        from context.manager import save_episodic_memory, get_conversation_context

        # 添加几条情景记忆
        save_episodic_memory("user1", "决策：微服务架构", importance=0.9,
                            tags=["decision"], category="user_decision")
        save_episodic_memory("user1", "完成 CI/CD 流水线搭建", importance=0.7,
                            tags=["task"], category="task_completion")

        result = get_conversation_context("user1", query="架构")
        assert result["status"] == "success"
        assert len(result["episodic"]) == 2
        assert "context_text" in result
        assert "情景记忆" in result["context_text"]
        assert "微服务架构" in result["context_text"]
        assert "CI/CD" in result["context_text"]

    def test_get_conversation_context_empty(self, clean_store, monkeypatch):
        from context.manager import get_conversation_context

        # Mock L3 search to return empty (since mem0 is mocked)
        monkeypatch.setattr(
            "context.manager.search_memory",
            lambda user_id, query, limit=10: {"status": "success", "results": [], "count": 0},
        )

        result = get_conversation_context("nonexistent_user")
        assert result["status"] == "success"
        assert result["episodic"] == []
        assert "无相关记忆" in result["context_text"]


# ═══════════════════════════════════════════════════
# 数据完整性测试
# ═══════════════════════════════════════════════════


class TestDataIntegrity:
    """验证存储文件格式和缓存一致性。"""

    @pytest.fixture
    def store(self):
        from context.episodic import EpisodicStore
        with tempfile.TemporaryDirectory() as tmpdir:
            s = EpisodicStore(data_dir=tmpdir)
            yield s
            s.clear_cache()

    def test_json_file_format(self, store):
        """验证写入的 JSON 文件格式。"""
        store.add("user1", "格式测试", importance=0.5)
        store.add("user1", "格式测试 2", importance=0.7)

        fp = store._file_path("user1")
        assert fp.exists()

        data = json.loads(fp.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 2
        for entry in data:
            assert "id" in entry
            assert "timestamp" in entry
            assert "summary" in entry
            assert "importance" in entry
            assert "tags" in entry
            assert "category" in entry
            assert "metadata" in entry

    def test_cache_invalidation(self, store):
        """clear_cache 后重新加载，数据应一致。"""
        store.add("user1", "缓存测试", importance=0.5)

        # 清除缓存
        store.clear_cache("user1")
        # 重新搜索应触发磁盘加载
        results = store.search("user1", query="缓存测试")
        assert len(results) == 1
        assert results[0]["summary"] == "缓存测试"

    def test_count_reflects_cache(self, store):
        assert store.count("user1") == 0
        store.add("user1", "测试", importance=0.5)
        assert store.count("user1") == 1
        store.delete_all("user1")
        assert store.count("user1") == 0
