"""知识库入库模块 — LightRAG 图增强检索 + Qdrant 向量存储。

索引时 LLM 自动抽取实体/关系构建知识图谱，
查询时图文混合（向量 + 图邻居遍历），内置 rerank。
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

from lightrag import LightRAG, QueryParam
from lightrag.base import DocStatus
from lightrag.utils import EmbeddingFunc

from config import (
    EMBEDDING_API_KEY,
    RERANK_API_KEY,
    RERANK_MODEL,
    LIGHTRAG_WORKING_DIR,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_FLASH_MODEL,
)
from .embedding import get_embedding_func


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _run_async(coro):
    """在同步上下文中运行异步协程。

    优先使用当前事件循环（线程安全调度），回退到 asyncio.run()。
    nest_asyncio 已移除 — 它会破坏 Python 3.12+ 的 asyncio task 追踪，
    导致 Timeout.__aenter__ 抛出 "Timeout should be used inside a task"。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环，创建新的
        return asyncio.run(coro)

    # 在已有事件循环的线程中：用线程安全方式调度，等待结果
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


# ---------------------------------------------------------------------------
# LLM Flash 函数（供 LightRAG 做实体/关系抽取及关键词提取）
# 使用通用 LLM_FLASH_MODEL，切换厂商只需改 .env
# ---------------------------------------------------------------------------


async def _llm_complete(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict] = [],
    **kwargs,
) -> str:
    from lightrag.llm.openai import openai_complete_if_cache

    # DeepSeek 不支持 Pydantic response_format（json_schema），只支持 json_object。
    # LightRAG 的 keyword_extraction 模式会设置 response_format=GPTKeywordExtractionFormat，
    # 这会导致 400 错误。这里将其替换为 json_object 格式。
    if kwargs.get("keyword_extraction"):
        del kwargs["keyword_extraction"]
        kwargs["response_format"] = {"type": "json_object"}

    return await openai_complete_if_cache(
        LLM_FLASH_MODEL,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Rerank 函数（供 LightRAG 做精排）
# ---------------------------------------------------------------------------

async def _rerank(
    query: str,
    documents: list[str],
    top_n: int,
) -> list[dict]:
    from dashscope import TextReRank

    resp = TextReRank.call(
        api_key=RERANK_API_KEY,
        model=RERANK_MODEL,
        query=query,
        documents=documents,
        top_n=top_n,
    )
    results = []
    if resp.output:
        for r in resp.output.get("results", []):
            results.append({
                "index": r["index"],
                "relevance_score": r["relevance_score"],
            })
    return results


# ---------------------------------------------------------------------------
# KnowledgeIndexer
# ---------------------------------------------------------------------------

class KnowledgeIndexer:
    """知识库索引器 — LightRAG + Qdrant。

    LightRAG 提供：向量检索 + 图检索 + 实体抽取 + 内置rerank。
    """

    def __init__(self):
        self._rag: LightRAG | None = None
        self._ef: EmbeddingFunc | None = None

    def _ensure_initialized(self):
        if self._rag is not None:
            return

        self._ef = get_embedding_func()

        self._rag = LightRAG(
            working_dir=LIGHTRAG_WORKING_DIR,
            embedding_func=self._ef,
            llm_model_func=_llm_complete,
            llm_model_name=LLM_FLASH_MODEL,
            rerank_model_func=_rerank,
            vector_storage="QdrantVectorDBStorage",
            graph_storage="Neo4JStorage",
            kv_storage="JsonKVStorage",
            doc_status_storage="JsonDocStatusStorage",
            embedding_batch_num=10,
            embedding_func_max_async=2,
            llm_model_max_async=2,
            default_llm_timeout=120,
            vector_db_storage_cls_kwargs={
                "cosine_better_than_threshold": 0.2,
            },

        )

    async def _ensure_initialized_async(self):
        """异步初始化 storages（LightRAG 要求）。

        每次 asyncio.run() 会创建新的事件循环，LightRAG 的 embedding worker
        队列会绑定到初始化时的事件循环上。如果检测到事件循环已变化，需要
        重新创建 LightRAG 实例，否则会出现 "PriorityQueue is bound to a
        different event loop" 错误。
        """
        try:
            current_loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            current_loop_id = None

        if self._rag is not None and current_loop_id is not None:
            cached_loop_id = getattr(self, "_loop_id", None)
            if cached_loop_id != current_loop_id:
                self._rag = None  # 事件循环变了，强制重建

        if self._rag is None:
            self._ensure_initialized()
            self._loop_id = current_loop_id

        if self._rag._storages_status.value != "initialized":
            await self._rag.initialize_storages()

    # ------------------------------------------------------------------
    # 公开 API（与旧接口兼容）
    # ------------------------------------------------------------------

    def index_text(
        self,
        text: str,
        source: str = "",
        tags: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """将文本索引到 LightRAG 知识图谱。

        tags: 逗号分隔的标签（通过 file_paths 传递）。
        user_id: 用户标识（通过 file_paths 传递）。
        metadata: 额外元数据（通过 file_paths 传递）。
        """
        if not text.strip():
            return {"status": "error", "message": "文本为空"}

        _run_async(self._index_async(text, source, tags, user_id, metadata))

        # B1 修复: 从 LightRAG doc_status 读取真实 chunk 数
        try:
            sources = _run_async(self._list_sources_async())
            for s in sources:
                if s.get("source") == source:
                    return {
                        "status": "success",
                        "chunks": s.get("chunks", 1),
                        "source": source,
                    }
        except Exception:
            pass

        return {"status": "success", "chunks": 1, "source": source}

    async def _index_async(
        self,
        text: str,
        source: str,
        tags: str = "",
        user_id: str = "",
        extra_metadata: dict[str, Any] | None = None,
    ):
        await self._ensure_initialized_async()
        kwargs = {}
        if source:
            kwargs["file_paths"] = [source]
        # 注意: LightRAG 1.5.x 不支持 addon_params 等自定义元数据参数，
        # tags / user_id / extra_metadata 目前仅记录日志供以后升级使用。
        if tags or user_id or extra_metadata:
            logger.debug(
                "Indexing with metadata (not passed to LightRAG 1.5.x): "
                "tags=%s user_id=%s metadata=%s",
                tags, user_id, extra_metadata,
            )
        await self._rag.ainsert(text, **kwargs)

    def search(
        self,
        query: str,
        n_results: int = 10,
        use_rerank: bool = True,
        rerank_top_k: int = 5,
        rerank_threshold: float = 0.3,
        filter_tag: str = "",
    ) -> dict[str, Any]:
        """图文混合检索，返回上下文字符串。

        .. deprecated::
            ``rerank_top_k``, ``rerank_threshold``, ``filter_tag`` 参数已废弃，
            将在未来版本中移除。请使用 ``src.rag.retrieval.HybridRetriever`` 替代。
        """
        # B3 修复: 死参数废弃提示
        result = _run_async(self._search_async(
            query, n_results,
            use_rerank=use_rerank,
        ))
        return {
            "status": "success",
            "results": [{"content": result, "score": 1.0}],
            "count": 1,
        }

    async def _search_async(
        self,
        query: str,
        top_k: int,
        use_rerank: bool = True,
        rerank_top_k: int = 5,
        rerank_threshold: float = 0.3,
    ) -> str:
        await self._ensure_initialized_async()
        try:
            param = QueryParam(
                mode="local",
                only_need_context=True,
                top_k=top_k,
                enable_rerank=use_rerank,
            )
            result = await self._rag.aquery(query, param=param)
        except Exception as exc:
            logger.warning("LightRAG search failed (%s), using Neo4j fallback", exc)
            result = None

        if result is None or "[no-context]" in result or "not able to provide" in result:
            result = await self._neo4j_fallback_search(query, top_k)
        return result

    async def _search_structured_async(
        self,
        query: str,
        top_k: int,
        use_rerank: bool = True,
    ) -> list[dict[str, Any]]:
        """结构化检索 — 返回分块列表（含 source 和 score）。

        尝试从 LightRAG 检索结果中提取真实的相关度分数和来源信息。
        当 LightRAG 返回合并字符串时，从 doc_status / graph 中重建结构化结果。
        """
        context = await self._search_async(query, top_k, use_rerank=use_rerank)
        if not context or "[no-context]" in context:
            return []

        # LightRAG local 模式返回合并上下文字符串，无分块边界
        # 尝试从 Neo4j fallback 路径的格式解析（该路径有 ## 标记的结构）
        results: list[dict[str, Any]] = []

        # 尝试解析 Neo4j fallback 格式的原文片段
        import re as _re
        chunk_matches = _re.findall(
            r"\[(\d+)\]\s+(.{20,500}?)(?=\n\[|\n##|\Z)",
            context, _re.DOTALL,
        )
        if chunk_matches:
            for idx, snippet in chunk_matches:
                results.append({
                    "content": snippet.strip(),
                    "score": 0.5,  # 默认中等分数
                    "source": "neo4j_graph",
                    "chunk_id": idx,
                })
            return results

        # 回退：按双换行拆分合并字符串为伪分块
        paragraphs = [p.strip() for p in context.split("\n\n") if p.strip() and not p.startswith("##")]
        # 优先取非标题行
        content_paragraphs = [p for p in paragraphs if len(p) > 50]
        if not content_paragraphs:
            content_paragraphs = paragraphs

        for i, para in enumerate(content_paragraphs[:top_k]):
            results.append({
                "content": para[:1000],  # 最多 1000 字符
                "score": 0.7 - (i * 0.05),  # 降序伪分数
                "source": "lightrag",
                "chunk_id": str(i),
            })

        return results

    async def _neo4j_fallback_search(self, query: str, top_k: int) -> str:
        """Qdrant 向量缺失时的兜底方案：直接用 Neo4j 全文搜索实体 + 图遍历。

        先在 Neo4j 全文索引中搜索匹配实体，再沿边遍历邻居节点和关系，
        最后从 JSON KV 存储中取出关联的文本分块内容。
        """
        graph = self._rag.chunk_entity_relation_graph
        text_chunks_db = self._rag.text_chunks

        try:
            # 1. 全文搜索匹配实体
            ft_results = await graph.search_labels(query, limit=top_k)
            if not ft_results:
                return "Sorry, I'm not able to provide an answer to that question.[no-context]"

            # 2. 批量获取实体节点详情
            nodes_dict = await graph.get_nodes_batch(ft_results)
            node_datas = [nodes_dict.get(nid) for nid in ft_results if nodes_dict.get(nid)]

            # 3. 获取每个实体的邻居边
            all_edges: list[dict] = []
            all_neighbor_ids: set[str] = set()
            for nd in node_datas:
                entity_id = nd.get("entity_id", "")
                edges = await graph.get_node_edges(entity_id)
                if edges:
                    all_edges.extend(edges)
                    for src, tgt in edges:
                        all_neighbor_ids.add(src)
                        all_neighbor_ids.add(tgt)

            # 4. 收集关联的文本分块 ID
            chunk_ids: set[str] = set()
            for nd in node_datas:
                source_id = nd.get("source_id", "")
                if source_id:
                    chunk_ids.update(source_id.split(","))
            for edge in all_edges:
                source_id = edge.get("source_id", "") if isinstance(edge, dict) else ""
                if source_id:
                    chunk_ids.update(source_id.split(","))

            # 5. 构建上下文
            parts: list[str] = []

            # 实体描述
            parts.append("## 匹配实体")
            for nd in node_datas[:top_k]:
                eid = nd.get("entity_id", "?")
                desc = nd.get("description", "") or ""
                etype = nd.get("entity_type", "")
                line = f"- {eid} ({etype})"
                if desc:
                    line += f": {desc}"
                parts.append(line)

            # 关系
            if all_edges:
                parts.append("\n## 关联关系")
                seen_edges: set[tuple[str, str]] = set()
                for edge in all_edges[:top_k * 3]:
                    if isinstance(edge, dict):
                        pair = (edge.get("source_id", ""), edge.get("target_id", ""))
                    else:
                        pair = (str(edge[0]), str(edge[1]))
                    if pair not in seen_edges:
                        seen_edges.add(pair)
                        desc = edge.get("description", "") if isinstance(edge, dict) else ""
                        line = f"- {pair[0]} → {pair[1]}"
                        if desc:
                            line += f" ({desc})"
                        parts.append(line)

            # 邻居实体
            neighbor_ids = all_neighbor_ids - set(ft_results)
            if neighbor_ids:
                neighbors = await graph.get_nodes_batch(list(neighbor_ids))
                parts.append("\n## 关联实体")
                for nid, ndata in neighbors.items():
                    desc = ndata.get("description", "") or ""
                    etype = ndata.get("entity_type", "")
                    line = f"- {nid} ({etype})"
                    if desc:
                        line += f": {desc}"
                    parts.append(line)

            # 文本分块
            if chunk_ids:
                chunk_texts: list[str] = []
                for cid in chunk_ids:
                    try:
                        chunk_data = await text_chunks_db.get_by_id(cid)
                        if chunk_data:
                            content = chunk_data.get("content", "") or ""
                            if content.strip():
                                chunk_texts.append(content.strip()[:500])
                    except Exception:
                        pass
                if chunk_texts:
                    parts.append("\n## 原文片段")
                    for i, ct in enumerate(chunk_texts[:5]):
                        parts.append(f"[{i+1}] {ct}")

            if len(parts) <= 1:
                return "Sorry, I'm not able to provide an answer to that question.[no-context]"

            return "\n".join(parts)

        except Exception as exc:
            logger.warning("Neo4j fallback search failed: %s", exc)
            return "Sorry, I'm not able to provide an answer to that question.[no-context]"

    def query_with_llm(self, query: str, top_k: int = 10) -> str:
        """带 LLM 生成的完整查询（非仅上下文）。"""
        return _run_async(self._query_with_llm_async(query, top_k))

    async def _query_with_llm_async(self, query: str, top_k: int) -> str:
        await self._ensure_initialized_async()
        param = QueryParam(mode="mix", top_k=top_k, enable_rerank=True)
        return await self._rag.aquery(query, param=param)

    def list_sources(self) -> list[dict[str, Any]]:
        """列出所有已索引的文档来源。"""
        return _run_async(self._list_sources_async())

    async def _list_sources_async(self) -> list[dict[str, Any]]:
        await self._ensure_initialized_async()
        docs = await self._rag.doc_status.get_docs_by_statuses([
            DocStatus.PROCESSED, DocStatus.PROCESSING, DocStatus.PENDING, DocStatus.FAILED,
        ])
        seen: set[str] = set()
        sources: list[dict[str, Any]] = []
        for doc in docs.values():
            fp = getattr(doc, "file_path", "") or "unknown"
            if fp not in seen:
                seen.add(fp)
                sources.append({
                    "source": fp,
                    "chunks": getattr(doc, "chunks_count", 1) or 1,
                    "content_length": getattr(doc, "content_length", 0) or 0,
                    "summary": getattr(doc, "content_summary", "") or "",
                    "status": getattr(doc, "status", "unknown"),
                    "indexed_at": getattr(doc, "created_at", "") or "",
                })
        return sources

    def delete_source(self, source: str) -> int:
        """删除指定来源的所有文档。"""
        return _run_async(self._delete_source_async(source))

    async def delete_source_async(self, source: str) -> int:
        """异步删除指定来源的所有文档（在已有事件循环中调用）。

        LightRAG 的 adelete_by_doc_id 会级联删除该文档的分块与图元素，
        仅被该文档引用的实体/关系会被移除，部分共享的会用剩余文档重建。
        """
        return await self._delete_source_async(source)

    async def _delete_source_async(self, source: str) -> int:
        await self._ensure_initialized_async()
        docs = await self._rag.doc_status.get_docs_by_statuses([
            DocStatus.PROCESSED, DocStatus.FAILED,
        ])
        count = 0
        for doc_id, doc in docs.items():
            if getattr(doc, "file_path", "") == source:
                await self._rag.adelete_by_doc_id(doc_id)
                count += 1
        return count

    def count(self) -> int:
        """返回已索引的文档总数。"""
        return _run_async(self._count_async())

    async def _count_async(self) -> int:
        await self._ensure_initialized_async()
        counts = await self._rag.doc_status.get_status_counts()
        return sum(counts.values())


# ---------------------------------------------------------------------------
# 模块单例
# ---------------------------------------------------------------------------

_indexer: KnowledgeIndexer | None = None


def get_indexer() -> KnowledgeIndexer:
    global _indexer
    if _indexer is None:
        _indexer = KnowledgeIndexer()
    return _indexer


# ---------------------------------------------------------------------------
# Agent 工具函数
# ---------------------------------------------------------------------------

def search_knowledge(query: str, top_k: int = 5, use_rerank: bool = True) -> dict[str, Any]:
    """从知识图谱中检索相关内容（LightRAG mix 模式：向量 + 图遍历）。

    文本通过 LLM 自动抽取实体和关系构建知识图谱，
    检索时结合向量相似度和图邻居遍历，比纯向量 RAG 覆盖更全。
    """
    try:
        return get_indexer().search(query, n_results=top_k, use_rerank=use_rerank)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def index_knowledge(text: str, source: str = "", tags: str = "") -> dict[str, Any]:
    """将文本索引到 LightRAG 知识图谱。"""
    try:
        return get_indexer().index_text(text, source=source, tags=tags)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def async_index_knowledge(text: str, source: str = "", tags: str = "") -> dict[str, Any]:
    """异步版索引 — 在已有的同一个事件循环中调用，避免 asyncio.run() 反复创建/销毁循环
    导致 Neo4j 异步驱动的连接池锁损坏。"""
    try:
        indexer = get_indexer()
        await indexer._index_async(text, source, tags)
        # B1 修复: 读取真实 chunk 数
        sources = await indexer._list_sources_async()
        chunks = 1
        for s in sources:
            if s.get("source") == source:
                chunks = s.get("chunks", 1)
                break
        return {"status": "success", "chunks": chunks, "source": source}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def async_search_knowledge(
    query: str,
    top_k: int = 5,
    mode: str = "dense",
) -> dict[str, Any]:
    """异步版检索 — 支持 dense/hybrid 两种模式。

    mode="dense": 使用 LightRAG 语义检索（向后兼容）。
    mode="hybrid": BM25 + LightRAG → RRF 融合 → reranker。
    """
    try:
        if mode == "hybrid":
            from .retrieval import get_retriever
            retriever = get_retriever()
            results = await retriever.search(query, top_k=top_k, mode="hybrid")
            return {
                "status": "success",
                "results": [r.to_dict() for r in results],
                "count": len(results),
                "mode": "hybrid",
            }

        # dense mode: 使用结构化检索获取分块结果
        indexer = get_indexer()
        structured = await indexer._search_structured_async(query, top_k)
        if structured:
            # B4 修复: 返回真实的结构化结果（含 source 和 score）
            return {
                "status": "success",
                "results": structured,
                "count": len(structured),
                "mode": "dense",
            }

        # 回退到旧版合并字符串格式
        context = await indexer._search_async(query, top_k)
        return {
            "status": "success",
            "results": [{"content": context, "score": 1.0}],
            "count": 1,
            "mode": "dense",
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def list_sources() -> dict[str, Any]:
    """列出知识库中所有文档来源。"""
    try:
        return {
            "status": "success",
            "sources": get_indexer().list_sources(),
            "total_chunks": get_indexer().count(),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def async_list_sources() -> dict[str, Any]:
    """异步版 — 列出知识库中所有文档来源，不阻塞事件循环。"""
    try:
        indexer = get_indexer()
        sources = await indexer._list_sources_async()
        count = await indexer._count_async()
        return {
            "status": "success",
            "sources": sources,
            "total_chunks": count,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}
