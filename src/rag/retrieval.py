"""混合检索引擎 — BM25 关键词 + Dense 向量 + 知识图谱 + RRF 融合。

检索流程：
  query → 并行 (BM25关键词 + LightRAG dense+graph)
  → RRF 融合 → DashScope reranker → top_k 结果

使用 BM25（rank_bm25）做关键词匹配，LightRAG 做语义召回，
Reciprocal Rank Fusion (RRF) 融合排序，DashScope 做精排。

注意：`_BM25Index` 是进程内内存索引，服务重启即丢。因此 BM25 通过「启动时从
LightRAG text_chunks 重建」（`KnowledgeIndexer.rebuild_bm25_async`，见
src/rag/indexer.py）+ 运行期增删同步（`_sync_bm25_source_async` 与
`_delete_source_async` 的 `remove_source`）来保持与持久化分块一致，**而不是**在
入库链路直接喂原始文本——LightRAG 入库后会二次切分，原始输入 ≠ 最终分块，只有
text_chunks 才是权威内容。据此 `mode="hybrid"` 的稀疏支路不再恒空。默认
`mode="dense"` 不受影响（agent 主链路 graph.py 仍硬编码 dense，hybrid 目前仅
经 /api/knowledge/search 与 tool_search_knowledge 可达）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """搜索结果。"""

    content: str
    score: float
    source: str = ""
    chunk_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "score": self.score,
            "source": self.source,
            "chunk_id": self.chunk_id,
        }


# ---------------------------------------------------------------------------
# BM25 索引
# ---------------------------------------------------------------------------


class _BM25Index:
    """内存 BM25 索引，支持增量添加和查询。

    使用 rank_bm25 库的 BM25Okapi 算法，对中文分词做简单字符级 n-gram。
    """

    def __init__(self):
        self._corpus: list[str] = []
        self._metadata: list[dict[str, Any]] = []
        self._bm25 = None
        self._dirty = False

    def add(self, text: str, metadata: dict[str, Any] | None = None) -> int:
        """添加文档，返回索引 ID。"""
        idx = len(self._corpus)
        self._corpus.append(text)
        self._metadata.append(metadata or {})
        self._dirty = True
        return idx

    def add_batch(self, texts: list[str], metadatas: list[dict[str, Any]] | None = None) -> None:
        """批量添加文档。"""
        for i, text in enumerate(texts):
            meta = metadatas[i] if metadatas else {}
            self._corpus.append(text)
            self._metadata.append(meta)
        self._dirty = True

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        """BM25 关键词搜索。"""
        if not self._corpus:
            return []

        self._ensure_index()
        tokenized_query = self._tokenize(query)
        scores = self._bm25.get_scores(tokenized_query)

        # 取 top_k
        indexed = list(enumerate(scores))
        indexed.sort(key=lambda x: x[1], reverse=True)
        top = indexed[:top_k]

        # rank_bm25 0.2.x 的 ATIRE BM25 变体在极小语料（< 10 docs）时
        # all-IDF-may-be-zero，导致全零分数。此时回退到简单子串匹配。
        max_score = max(scores) if len(scores) > 0 else 0

        results = []
        if max_score <= 0:
            # 回退: token 级别子串匹配（每个 query token 独立匹配 corpus）
            query_lower = query.lower()
            query_tokens = tokenized_query
            scored: list[tuple[int, float]] = []
            for idx, doc in enumerate(self._corpus):
                doc_lower = doc.lower()
                # 计算命中 query token 的比例
                hits = sum(1 for t in query_tokens if t in doc_lower)
                if hits > 0:
                    scored.append((idx, hits / len(query_tokens)))
            scored.sort(key=lambda x: x[1], reverse=True)
            for idx, s in scored[:top_k]:
                meta = self._metadata[idx] if idx < len(self._metadata) else {}
                results.append(SearchResult(
                    content=self._corpus[idx],
                    score=s * 0.5,  # 回退分数打折，低于 BM25 正常分数
                    source=meta.get("source", ""),
                    chunk_id=meta.get("chunk_id", str(idx)),
                    metadata=meta,
                ))
        else:
            for idx, score in top:
                if score <= 0:
                    continue
                meta = self._metadata[idx] if idx < len(self._metadata) else {}
                results.append(SearchResult(
                    content=self._corpus[idx],
                    score=min(score / max_score, 1.0),
                    source=meta.get("source", ""),
                    chunk_id=meta.get("chunk_id", str(idx)),
                    metadata=meta,
                ))
        return results

    def remove_by_source(self, source: str) -> int:
        """删除指定来源的所有文档。"""
        to_keep: list[int] = []
        removed = 0
        for i, meta in enumerate(self._metadata):
            if meta.get("source") == source:
                removed += 1
            else:
                to_keep.append(i)

        if removed > 0:
            self._corpus = [self._corpus[i] for i in to_keep]
            self._metadata = [self._metadata[i] for i in to_keep]
            self._dirty = True
        return removed

    def clear(self) -> None:
        """清空索引。"""
        self._corpus.clear()
        self._metadata.clear()
        self._bm25 = None
        self._dirty = False

    def _ensure_index(self):
        if not self._dirty or not self._corpus:
            return
        try:
            from rank_bm25 import BM25Okapi
            tokenized = [self._tokenize(doc) for doc in self._corpus]
            self._bm25 = BM25Okapi(tokenized)
            self._dirty = False
        except ImportError:
            logger.warning("rank_bm25 未安装，BM25 搜索不可用")
            self._bm25 = None
            self._dirty = False

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文 + 英文混合分词。

        对中文做字符级 bigram，对英文/数字做空格分词。
        """
        import re
        tokens: list[str] = []
        # 按非字母数字分割
        segments = re.split(r"([a-zA-Z0-9]+)", text)
        for seg in segments:
            if not seg:
                continue
            if re.match(r"[a-zA-Z0-9]+", seg):
                # 英文/数字 → 转小写 + 原样
                tokens.append(seg.lower())
            else:
                # 中文 → bigram
                seg = seg.strip()
                for i in range(len(seg)):
                    # unigram
                    ch = seg[i]
                    if not ch.isspace():
                        tokens.append(ch)
                    # bigram
                    if i < len(seg) - 1:
                        tokens.append(seg[i:i + 2])
        return tokens

    def __len__(self) -> int:
        return len(self._corpus)


# ---------------------------------------------------------------------------
# 混合检索引擎
# ---------------------------------------------------------------------------


class HybridRetriever:
    """BM25 关键词 + LightRAG 语义 + 知识图谱 多路混合检索。

    检索模式:
      - "hybrid": 默认，BM25 + LightRAG dense + graph → RRF 融合
      - "dense": 仅 LightRAG dense+graph 语义检索
      - "sparse": 仅 BM25 关键词检索
      - "graph": 仅 LightRAG 图遍历（Neo4j 全文搜索）
    """

    def __init__(self):
        self._bm25 = _BM25Index()

    # ── 索引 ──

    def index_text(self, text: str, source: str = "", chunk_id: str = "") -> int:
        """将文本加入 BM25 索引。"""
        return self._bm25.add(text, {"source": source, "chunk_id": chunk_id})

    def index_texts(self, texts: list[str], source: str = "", chunk_ids: list[str] | None = None) -> None:
        """批量加入 BM25 索引。"""
        metas = []
        for i, text in enumerate(texts):
            cid = chunk_ids[i] if chunk_ids and i < len(chunk_ids) else str(i)
            metas.append({"source": source, "chunk_id": cid})
        self._bm25.add_batch(texts, metas)

    def remove_source(self, source: str) -> int:
        """移除 BM25 索引中指定来源的文档。"""
        return self._bm25.remove_by_source(source)

    def rebuild(
        self,
        texts: list[str],
        sources: list[str] | None = None,
        chunk_ids: list[str] | None = None,
    ) -> int:
        """清空并用给定语料重建 BM25 索引（启动时从 LightRAG text_chunks 同步）。

        与 index_texts 的区别：先 clear() 再批量填充，保证索引与持久化分块完全
        一致，避免多次增量 add 造成残留/漂移。返回索引文档数。
        """
        self._bm25.clear()
        metas: list[dict[str, Any]] = []
        for i, text in enumerate(texts):
            source = sources[i] if sources and i < len(sources) else ""
            cid = chunk_ids[i] if chunk_ids and i < len(chunk_ids) else str(i)
            metas.append({"source": source, "chunk_id": cid})
        self._bm25.add_batch(texts, metas)
        return len(texts)

    def bm25_count(self) -> int:
        """BM25 索引当前文档数（供验证/观测）。"""
        return len(self._bm25)

    # ── 检索 ──

    async def search(
        self,
        query: str,
        top_k: int = 10,
        mode: str = "hybrid",
        use_rerank: bool = True,
    ) -> list[SearchResult]:
        """混合检索。

        Args:
            query: 查询文本。
            top_k: 返回结果数。
            mode: "hybrid" | "dense" | "sparse" | "graph"
            use_rerank: 是否使用 DashScope reranker 精排。

        Returns:
            list[SearchResult] 按相关度降序排列。
        """
        import asyncio

        if mode == "sparse":
            return self._bm25.search(query, top_k=top_k)

        if mode == "graph":
            return await self._graph_search(query, top_k)
            return results

        if mode == "dense":
            return await self._dense_search(query, top_k)

        # mode == "hybrid": 并行 BM25 + LightRAG dense+graph
        dense_results, sparse_results = await asyncio.gather(
            self._dense_search(query, top_k * 2),
            asyncio.to_thread(self._bm25.search, query, top_k * 2),
        )

        # RRF 融合
        merged = self._rrf_fuse(sparse_results, dense_results, k=60)

        # 取 top_k * 2 送 reranker
        candidates = merged[:top_k * 2]

        if use_rerank and candidates:
            try:
                reranked = await self._rerank(query, candidates, top_k)
                return reranked
            except Exception:
                logger.warning("Rerank 失败，返回 RRF 融合结果", exc_info=True)

        return candidates[:top_k]

    # ── 内部 ──

    async def _dense_search(self, query: str, top_k: int) -> list[SearchResult]:
        """LightRAG dense + graph 检索。

        优先走结构化检索（分块 + 真实来源/分数），回退到合并字符串兜底。
        修复：此前把 LightRAG 合并字符串整体包成单条结果（score=1.0、chunk_id=0），
        导致 chunks 恒为 1、`[N]`/`<SEP>`/代码围栏等污染直接进入上下文。
        """
        from rag.indexer import get_indexer
        try:
            # 关闭 LightRAG 内部 rerank：hybrid 模式在 RRF 融合后会再走一次外层 reranker，
            # 保留此处会导致双重精排（LightRAG 内部 + 外层 HybridRetriever），白白多花一轮。
            structured = await get_indexer()._search_structured_async(query, top_k, use_rerank=False)
            if structured:
                return [
                    SearchResult(
                        content=self._clean_context(str(item.get("content", ""))),
                        score=float(item.get("score", 0.5)),
                        source=str(item.get("source", "lightrag")),
                        chunk_id=str(item.get("chunk_id", "")),
                    )
                    for item in structured
                ]
        except Exception:
            logger.warning("LightRAG structured search failed, falling back", exc_info=True)

        # 兜底：合并字符串（清洗后单条返回）
        try:
            result = await get_indexer()._search_async(query, top_k)
            cleaned = self._clean_context(result or "")
            if cleaned and "[no-context]" not in cleaned:
                return [SearchResult(
                    content=cleaned,
                    score=0.5,
                    source="lightrag_dense",
                    chunk_id="0",
                )]
        except Exception:
            logger.warning("LightRAG dense search failed", exc_info=True)
        return []

    @staticmethod
    def _clean_context(text: str) -> str:
        """清洗 LightRAG 合并上下文字符串：去掉 [N] 索引、<SEP> 分隔符、代码围栏、多余空行。"""
        import re
        if not text:
            return ""
        # 去掉行首的 [数字] 引用标记
        text = re.sub(r"^\s*\[\d+\]\s*", "", text, flags=re.MULTILINE)
        # <SEP> 分隔符 → 换行
        text = text.replace("<SEP>", "\n")
        # 代码围栏（```python 等）
        text = re.sub(r"```[a-zA-Z0-9_-]*", "", text)
        # 折叠多余空行
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    async def _graph_search(query: str, top_k: int) -> list[SearchResult]:
        """Neo4j 图遍历检索。"""
        from rag.indexer import get_indexer
        try:
            result = await get_indexer()._neo4j_fallback_search(query, top_k)
            if result and "[no-context]" not in result:
                return [SearchResult(
                    content=result,
                    score=0.8,
                    source="neo4j_graph",
                    chunk_id="0",
                )]
        except Exception:
            logger.warning("Neo4j graph search failed", exc_info=True)
        return []

    @staticmethod
    async def _rerank(query: str, results: list[SearchResult], top_n: int) -> list[SearchResult]:
        """DashScope reranker 精排。"""
        from dashscope import TextReRank
        from config import RERANK_API_KEY, RERANK_MODEL

        if not results:
            return []

        documents = [r.content for r in results]
        resp = TextReRank.call(
            api_key=RERANK_API_KEY,
            model=RERANK_MODEL,
            query=query,
            documents=documents,
            top_n=min(top_n, len(documents)),
        )

        reranked: list[SearchResult] = []
        if resp.output:
            for item in resp.output.get("results", []):
                idx = item.get("index", 0)
                score = item.get("relevance_score", 0)
                if 0 <= idx < len(results):
                    r = results[idx]
                    r.score = score
                    reranked.append(r)

        return reranked[:top_n]

    @staticmethod
    def _rrf_fuse(
        results_a: list[SearchResult],
        results_b: list[SearchResult],
        k: int = 60,
    ) -> list[SearchResult]:
        """Reciprocal Rank Fusion 融合两组排序结果。

        RRF score = Σ 1 / (k + rank)

        Args:
            results_a: 第一组结果（如 BM25）。
            results_b: 第二组结果（如 Dense）。
            k: 平滑参数，默认 60。

        Returns:
            融合后的结果，按 RRF 分数降序排列。
        """
        rrf_scores: dict[str, tuple[float, SearchResult]] = {}

        for rank, result in enumerate(results_a):
            key = result.content.strip()[:200]  # 用内容前 200 字符做 key
            score = 1.0 / (k + rank + 1)
            if key in rrf_scores:
                rrf_scores[key] = (rrf_scores[key][0] + score, rrf_scores[key][1])
            else:
                rrf_scores[key] = (score, result)

        for rank, result in enumerate(results_b):
            key = result.content.strip()[:200]
            score = 1.0 / (k + rank + 1)
            if key in rrf_scores:
                rrf_scores[key] = (rrf_scores[key][0] + score, rrf_scores[key][1])
            else:
                rrf_scores[key] = (score, result)

        fused = sorted(rrf_scores.values(), key=lambda x: x[0], reverse=True)
        final: list[SearchResult] = []
        for total_score, result in fused:
            result.score = total_score
            final.append(result)
        return final


# ---------------------------------------------------------------------------
# 模块单例
# ---------------------------------------------------------------------------


_retriever: HybridRetriever | None = None


def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever
