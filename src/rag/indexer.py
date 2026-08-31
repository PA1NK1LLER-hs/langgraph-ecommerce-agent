"""知识库入库模块 — LightRAG 图增强检索 + Qdrant 向量存储。

索引时 LLM 自动抽取实体/关系构建知识图谱，
查询时图文混合（向量 + 图邻居遍历），内置 rerank。
"""

import asyncio
import logging
import uuid
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


def _clean_context(text: str) -> str:
    """清洗 LightRAG 合并上下文字符串：去掉 [N] 索引、<SEP> 分隔符、代码围栏、多余空行。

    LightRAG local 模式返回的合并上下文常夹带引用标记与分隔符，直接进入
    提示词会造成乱码/污染。此处统一清洗后再返回给调用方。
    """
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


# 行级切分时，同一文件的多个数据行以「文件名::Sheet::行号」作为唯一 file_path 入库，
# 以绕过 LightRAG 按 file_path 判重（否则同文件的多行会被误判为「文件名重复」而只索引
# 第一行）。此处用「::」作分隔——Windows 文件名与 Excel sheet 名均不允许冒号，故安全。
ROW_DELIMITER = "::"


def _clean_source_name(file_path: str) -> str:
    """从行级切分的 file_path 还原干净文件名（去掉 ``::Sheet::行号`` 后缀）。

    对非行级切分的普通来源（无 ``::``）原样返回，因此对 upload/reindex 路径透明。
    """
    if not file_path:
        return "unknown"
    return file_path.split(ROW_DELIMITER)[0]


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

_RERANK_POOL_IDLE_SECONDS = 30.0  # 连接池空闲超过该秒数才清池（应短于服务端 keep-alive 闲置超时）
_rerank_last_use = 0.0


def _reset_rerank_pool_if_idle() -> None:
    """连接池空闲超时后才清池，避免频繁清池带来的重复 TLS 握手开销。

    根因（已定位到 SDK 源码）：dashscope 的 http_request 模块用进程级共享
    requests.Session 做连接池（`_get_shared_sync_session`，注释明确「never closed
    explicitly」），长运行后旧 keep-alive 连接被远端/NAT 闲置超时掐断，再次复用
    即 ConnectionResetError 10054。实测 Connection: close 头在本网关不可靠（服务端
    不回显），故用「空闲超时清池」策略：高频连续查询复用连接（实测 ~0.4s/次），
    空闲超过阈值才 pm.clear() 重建连接（~2s/次，但只发生在闲置后的首次查询，且
    杜绝了 20s 级的 ConnectionReset 卡顿）。该 Session 进程全局，仅 rerank 走
    dashscope 同步调用（LLM/embedding 走 openai SDK），互不影响。
    """
    global _rerank_last_use
    import time as _time
    now = _time.monotonic()
    if _rerank_last_use and (now - _rerank_last_use) < _RERANK_POOL_IDLE_SECONDS:
        _rerank_last_use = now
        return
    try:
        from dashscope.api_entities import http_request
        sess = http_request._get_shared_sync_session()
        for adapter in sess.adapters.values():
            pm = getattr(adapter, "poolmanager", None)
            if pm is not None and hasattr(pm, "clear"):
                pm.clear()
    except Exception:
        pass
    _rerank_last_use = now


async def _rerank(
    query: str,
    documents: list[str],
    top_n: int,
) -> list[dict]:
    from dashscope import TextReRank
    import requests as _requests

    _reset_rerank_pool_if_idle()
    # 同步阻塞调用放线程池，避免冻结事件循环；连接抖动时重试一次
    resp = None
    for attempt in (1, 2):
        try:
            resp = await asyncio.to_thread(
                TextReRank.call,
                api_key=RERANK_API_KEY,
                model=RERANK_MODEL,
                query=query,
                documents=documents,
                top_n=top_n,
            )
            break
        except (_requests.exceptions.RequestException, OSError) as exc:
            # ConnectionResetError 等网络层错误：重试一次，仍失败则回退原始分块
            if attempt == 2:
                logger.warning("rerank 连续两次失败，回退原始分块: %s", exc)
                return []
            await asyncio.sleep(0.2)

    results = []
    if resp is not None and resp.output:
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
            embedding_batch_num=30,
            embedding_func_max_async=4,
            llm_model_max_async=8,
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

    async def _invalidate_cache(self) -> None:
        """KB 内容变更后失效语义缓存，避免同一问题重复问返回陈旧答案。

        call_model 节点按 user_text 精确命中缓存（TTL 3600s），若不清空，
        上传/删除/重建知识库后用户再问同一个问题，会直接拿到旧回答（跳过
        RAG 检索与 LLM）。此方法幂等，Redis 不可用时静默降级。
        """
        try:
            from cache.semantic_cache import get_cache
            await get_cache().clear()
        except Exception:
            logger.debug("语义缓存失效失败", exc_info=True)

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
        doc_id: str | None = None,
    ):
        await self._ensure_initialized_async()
        # 关键：file_path 必须唯一。LightRAG 1.5.x 入库时按 file_path（文件名）判重，
        # 同一文件的多行若共用干净文件名，除第一行外都会被误判为「文件名重复」而丢弃。
        # doc_id 缺失时（upload/reindex/import-url/tool 逐块入库）自动追加唯一后缀，
        # 展示/删除时再用 _clean_source_name 还原干净文件名。
        unique_fp = doc_id or (f"{source}::{uuid.uuid4().hex}" if source else None)
        ids = [unique_fp] if unique_fp else None
        file_paths = [unique_fp] if unique_fp else None
        # 注意: LightRAG 1.5.x 不支持 addon_params 等自定义元数据参数，
        # tags / user_id / extra_metadata 目前仅记录日志供以后升级使用。
        if tags or user_id or extra_metadata:
            logger.debug(
                "Indexing with metadata (not passed to LightRAG 1.5.x): "
                "tags=%s user_id=%s metadata=%s",
                tags, user_id, extra_metadata,
            )
        # process_options="!"（skip_kg）：只切分 + 向量化，跳过 LLM 实体/关系抽取。
        # 表格类数据（产品明细/对照表）无需知识图谱，抽取既慢（易 LLM 超时）又产生
        # 「Sheet1」这类噪声实体；向量分块检索已足够支撑「负责人→产品」等查表类问题。
        await self._rag.apipeline_enqueue_documents(
            text,
            ids=ids,
            file_paths=file_paths,
            process_options="!",
        )
        await self._rag.apipeline_process_enqueue_documents()
        await self._invalidate_cache()
        await self._sync_bm25_source_async(source)

    async def _index_rows_async(
        self,
        rows: list[tuple[str, str, str]],
        batch_size: int = 50,
    ) -> int:
        """批量行级索引 — 把多行合并到一次管道调用。

        每行 ``(text, source, doc_id)``：source 是干净文件名，doc_id 唯一
        （文件名::Sheet::行号）。逐行入库时，JsonKVStorage 每处理一个文档就
        整库写盘一次（实体/关系 JSON 随行数增长，整体 O(n²)）。批量入库把多行
        交给一次管道处理，持久化按批摊薄。

        用 process_options="!"（skip_kg）跳过 LLM 实体/关系抽取：表格类数据
        抽取每行要数秒且常超时（240s），2899 行全量抽取需数天；跳过抽取后只做
        切分 + 向量化，几分钟即可完成，向量分块检索已足够支撑查表类问题。

        Returns: 实际索引的文本行数。
        """
        await self._ensure_initialized_async()
        total = 0
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            texts = [r[0] for r in batch]
            ids = [r[2] for r in batch]
            # file_path 用唯一 doc_id 绕过去重；展示/删除时 _clean_source_name 还原干净文件名
            await self._rag.apipeline_enqueue_documents(
                texts,
                ids=ids,
                file_paths=ids,
                process_options="!",
            )
            await self._rag.apipeline_process_enqueue_documents()
            total += len(texts)
        await self._invalidate_cache()
        # 同步 BM25：入库走 LightRAG 二次切分，最终分块以 text_chunks 为准
        for _src in {r[1] for r in rows if r[1]}:
            await self._sync_bm25_source_async(_src)
        return total

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

    # 全中文短问改用本地关键词的字符数阈值（超过则走 LLM 关键词提取）
    _LOCAL_KEYWORD_MAX_LEN = 100

    @staticmethod
    def _should_use_local_keywords(query: str) -> bool:
        """判断是否用本地关键词（跳过 LLM 关键词提取）。

        规则（方案B自适应）：
          - 全中文 且 长度 ≤ 100 字 → 本地关键词（LLM 关键词提取是查询耗时主因）
          - 含英文（中英混合）或 超过 100 字 → LLM 关键词提取（本地 bigram 覆盖不足）
        """
        import re
        if len(query) > KnowledgeIndexer._LOCAL_KEYWORD_MAX_LEN:
            return False
        return not re.search(r"[a-zA-Z]", query)

    @staticmethod
    def _local_keywords(query: str) -> list[str]:
        """本地关键词生成：中文 bigram+trigram + 英文/数字词，去重保序。

        用于全中文短问的提速——LLM 关键词提取是查询耗时主因（冷启动 17-35s），
        对短中文查询用字符级 n-gram 生成候选关键词，近零开销。
        bigram 覆盖二字词，trigram 覆盖三字人名/术语（如「郑钰莹」整体命中）。

        注意：LightRAG local 模式只用 ll_keywords 检索图实体；向量分块检索仍用
        完整 query 的 embedding，因此这里的关键词只影响图实体召回，不影响向量分块召回。
        """
        import re
        tokens: list[str] = []
        segments = re.split(r"([a-zA-Z0-9]+)", query)
        for seg in segments:
            if not seg:
                continue
            if re.match(r"[a-zA-Z0-9]+", seg):
                tokens.append(seg.lower())
            else:
                seg = re.sub(r"\s+", "", seg)
                for n in (2, 3):
                    for i in range(len(seg) - n + 1):
                        tokens.append(seg[i:i + n])
        seen: set[str] = set()
        out: list[str] = []
        for t in tokens:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out[:100]

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
            # 方案B提速：全中文短问用本地关键词跳过 LLM 关键词提取（耗时主因），
            # 含英文/超长问改走 LLM 关键词提取。若本地关键词为空（极短输入）则
            # 不传，让 LightRAG 回退到默认提取。
            param_kwargs: dict[str, Any] = {}
            if self._should_use_local_keywords(query):
                local_kws = self._local_keywords(query)
                if local_kws:
                    param_kwargs["ll_keywords"] = local_kws
                    logger.debug("本地关键词（跳过 LLM 提取）: %s", local_kws[:10])

            # 用 mix 而非 local：入库用 process_options="!"（skip_kg）跳过了实体/关系
            # 抽取，图是空的，local 模式只检索图实体会返回空。mix 模式在图实体/关系
            # 为空时仍会做向量分块检索（_get_vector_context），正是表格类查表问题所需。
            param = QueryParam(
                mode="mix",
                only_need_context=True,
                top_k=top_k,
                enable_rerank=use_rerank,
                **param_kwargs,
            )
            result = await self._rag.aquery(query, param=param)
        except Exception as exc:
            logger.warning("LightRAG search failed (%s), using Neo4j fallback", exc)
            result = None

        if result is None or "[no-context]" in result or "not able to provide" in result:
            result = await self._neo4j_fallback_search(query, top_k)
        return result

    @staticmethod
    def _parse_lightrag_document_chunks(context: str) -> list[dict[str, Any]]:
        """从 LightRAG local 模式返回里解析真实的 Document Chunks JSON 块。

        LightRAG 1.5.x 返回结构（尾部）：
            Document Chunks (...):
            ```json
            {"reference_id": "1", "content": "..."}
            {"reference_id": "2", "content": "..."}
            ```
            Reference Document List (...):
            [1] 各站点ASIN-评论星级用.xlsx
            [2] 各站点ASIN-评论星级用 - 天安.xlsx

        这里提取每行 JSON 的 content 作为分块，并把 reference_id 映射成真实文件名
        作为 source。此前用正则 `[数字]` 匹配「原文片段」时，会误把末尾 Reference
        Document List 里的 `[1] 文件名` 当成 chunk 内容，导致真正的表格内容被丢弃、
        只剩下文件名（例如「郑钰莹」人名查得回来但内容为空）。
        """
        import json as _json
        import re as _re

        # 1. Reference Document List → {ref_id: 文件名}
        ref_map: dict[str, str] = {}
        ref_block = _re.search(
            r"Reference Document List[^\n]*:\s*\n+```?\s*\n(.*?)```",
            context, _re.DOTALL,
        )
        if ref_block:
            for line in ref_block.group(1).splitlines():
                m = _re.match(r"\[(\d+)\]\s+(.+)", line.strip())
                if m:
                    ref_map[m.group(1)] = m.group(2).strip()

        # 2. Document Chunks 的 ```json 块 → 逐行解析 content
        chunks: list[dict[str, Any]] = []
        m = _re.search(
            r"Document Chunks[^\n]*:\s*\n+```json\s*\n(.*?)```",
            context, _re.DOTALL,
        )
        if not m:
            return chunks
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = _json.loads(line)
            except Exception:
                continue
            content = str(obj.get("content", "") or "").strip()
            if not content:
                continue
            ref_id = str(obj.get("reference_id", ""))
            chunks.append({
                "content": content,
                "source": _clean_source_name(ref_map.get(ref_id, "")),
                "chunk_id": ref_id,
            })
        return chunks

    async def _search_structured_async(
        self,
        query: str,
        top_k: int,
        use_rerank: bool = True,
    ) -> list[dict[str, Any]]:
        """结构化检索 — 返回分块列表（含 source 和 score）。

        优先解析 LightRAG local 模式返回的 Document Chunks（真实分块 + 来源文件）；
        失败时回退到 Neo4j 兜底格式的「## 原文片段」；再失败按段落切分。
        """
        context = await self._search_async(query, top_k, use_rerank=use_rerank)
        if not context or "[no-context]" in context:
            return []

        results: list[dict[str, Any]] = []

        # 优先：LightRAG Document Chunks JSON（含真实内容与来源）
        # 注意：不要用 2000 这类小上限截断——表格类 chunk 会把整张 sheet 塞进一个
        # chunk（可达数千字符），人名/产品行常落在 chunk 末尾，截断会丢数据
        #（例如「郑钰莹」5 行产品在 2590 字符的 chunk 第 1834 字符起，2000 截断只剩 3 行）。
        chunks = self._parse_lightrag_document_chunks(context)
        if chunks:
            for i, c in enumerate(chunks[:top_k]):
                results.append({
                    "content": _clean_context(c["content"])[:8000],
                    "score": 0.7 - (i * 0.03),  # 降序伪分数
                    "source": c["source"] or "lightrag",
                    "chunk_id": c["chunk_id"],
                })
            return results

        # 回退 1：Neo4j fallback 的「## 原文片段」里的 [数字] 片段
        import re as _re
        src_sec = _re.search(r"##\s*原文片段\s*\n(.*)", context, _re.DOTALL)
        if src_sec:
            chunk_matches = _re.findall(
                r"\[(\d+)\]\s+(.{20,500}?)(?=\n\[|\Z)",
                src_sec.group(1), _re.DOTALL,
            )
            if chunk_matches:
                for idx, snippet in chunk_matches:
                    results.append({
                        "content": _clean_context(snippet),
                        "score": 0.5,
                        "source": "neo4j_graph",
                        "chunk_id": idx,
                    })
                return results

        # 回退 2：按双换行拆分合并字符串为伪分块
        paragraphs = [p.strip() for p in context.split("\n\n") if p.strip() and not p.startswith("##")]
        content_paragraphs = [p for p in paragraphs if len(p) > 50]
        if not content_paragraphs:
            content_paragraphs = paragraphs

        for i, para in enumerate(content_paragraphs[:top_k]):
            results.append({
                "content": _clean_context(para[:1000]),
                "score": 0.7 - (i * 0.05),
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
        # 行级切分后同一文件会拆成多个文档（每个数据行一个 doc_id，file_path 为
        # 「文件名::Sheet::行号」）。这里按干净文件名聚合，让来源列表保持「一个文件
        # 一条」，并累加 chunks/content_length。
        grouped: dict[str, dict[str, Any]] = {}
        for doc in docs.values():
            fp = getattr(doc, "file_path", "") or "unknown"
            clean = _clean_source_name(fp)
            if clean not in grouped:
                grouped[clean] = {
                    "source": clean,
                    "chunks": 0,
                    "content_length": 0,
                    "summary": getattr(doc, "content_summary", "") or "",
                    "status": getattr(doc, "status", "unknown"),
                    "indexed_at": getattr(doc, "created_at", "") or "",
                }
            grouped[clean]["chunks"] += getattr(doc, "chunks_count", 1) or 1
            grouped[clean]["content_length"] += getattr(doc, "content_length", 0) or 0
        return list(grouped.values())

    async def _list_source_chunks_async(self, source: str) -> list[dict[str, Any]]:
        """列出指定来源（干净文件名）的真实分块，按 chunk_order_index 排序。

        直接读 text_chunks KV 存储（每个分块一行，含 full_doc_id / file_path 指向
        其所属文档），按干净文件名过滤——这才是「查看分块」该返回的数据。此前
        api_get_source_chunks 用语义搜索冒充，返回的是「与文件名向量最相似」的
        无关片段（文件名作 query，向量检索召回的是语义近邻而非该文档的分块）。
        """
        await self._ensure_initialized_async()
        text_chunks = self._rag.text_chunks
        chunks: list[dict[str, Any]] = []
        try:
            keys = list(text_chunks._data.keys())
        except Exception:
            return chunks
        for key in keys:
            row = await text_chunks.get_by_id(key)
            if not row:
                continue
            fp = row.get("full_doc_id") or row.get("file_path") or ""
            if _clean_source_name(fp) != source:
                continue
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            order = row.get("chunk_order_index")
            chunks.append({
                "content": content,
                "source": source,
                "chunk_id": key,
                "order": order if isinstance(order, int) else 0,
            })
        chunks.sort(key=lambda c: c["order"])
        return chunks

    async def _read_all_chunks_async(self) -> list[dict[str, Any]]:
        """读全部最终分块（content/source/chunk_id），供 BM25 重建使用。

        直接遍历 LightRAG text_chunks KV 存储，与 _list_source_chunks_async 同源，
        但跨全部来源。source 用干净文件名（与删除/引用聚合一致）。
        """
        await self._ensure_initialized_async()
        text_chunks = self._rag.text_chunks
        chunks: list[dict[str, Any]] = []
        try:
            keys = list(text_chunks._data.keys())
        except Exception:
            return chunks
        for key in keys:
            try:
                row = await text_chunks.get_by_id(key)
            except Exception:
                continue
            if not row:
                continue
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            fp = row.get("full_doc_id") or row.get("file_path") or ""
            chunks.append({
                "content": content,
                "source": _clean_source_name(fp),
                "chunk_id": key,
            })
        return chunks

    async def rebuild_bm25_async(self) -> int:
        """从 LightRAG text_chunks 重建进程内 BM25 索引（启动/重建后同步）。

        只有 text_chunks 才是 LightRAG 二次切分后的最终分块，故以此为唯一权威源，
        不在入库链路直接喂原始文本（原始输入会被二次切分，二者不一致）。
        幂等：text_chunks 为空时保持索引为空并返回 0。
        """
        from .retrieval import get_retriever
        chunks = await self._read_all_chunks_async()
        if not chunks:
            logger.warning("BM25 重建：text_chunks 为空，索引保持为空")
            return 0
        count = get_retriever().rebuild(
            [c["content"] for c in chunks],
            [c["source"] for c in chunks],
            [c["chunk_id"] for c in chunks],
        )
        logger.info("BM25 索引重建完成: %d 个分块", count)
        return count

    async def _sync_bm25_source_async(self, source: str) -> None:
        """运行期入库后，把某来源的最终分块同步进 BM25（先移除旧条目再重加）。

        best-effort：失败只记日志，不影响主索引/删除链路。
        """
        if not source:
            return
        try:
            from .retrieval import get_retriever
            retriever = get_retriever()
            retriever.remove_source(source)
            chunks = await self._list_source_chunks_async(source)
            if not chunks:
                return
            retriever.index_texts(
                [c["content"] for c in chunks],
                source=source,
                chunk_ids=[c["chunk_id"] for c in chunks],
            )
        except Exception:
            logger.warning("BM25 同步来源 %s 失败", source, exc_info=True)

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
            if _clean_source_name(getattr(doc, "file_path", "")) == source:
                await self._rag.adelete_by_doc_id(doc_id)
                count += 1
        if count:
            await self._invalidate_cache()
            from .retrieval import get_retriever
            removed = get_retriever().remove_source(source)
            logger.info("BM25 移除来源 %s: %d 条", source, removed)
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
    use_rerank: bool = False,
) -> dict[str, Any]:
    """异步版检索 — 支持 dense/hybrid 两种模式。

    mode="dense": 使用 LightRAG 语义检索（向后兼容）。
    mode="hybrid": BM25 + LightRAG → RRF 融合（use_rerank 固定 False，见下）。
    use_rerank: dense 模式下是否启用 LightRAG 内置 rerank（DashScope）。
        默认关闭：内置 rerank 走 DashScope 原生端点，长连接在服务长时间运行后
        会间歇性被远端重置（ConnectionResetError 10054），触发重试拖慢检索；
        且失败后本就回退「原始分块」。注意：rerank 会重排 chunk 顺序（精排），
        并非零影响——只是对当前「行级切分的表格库 + 查表类问题」而言，top-5
        内容基本是同一张表的行，重排后实际答案无实质差异，故默认关闭只省
        延迟与报错、不丢答案；大库/多文档交叉/长文档场景如需恢复再打开。
    """
    try:
        if mode == "hybrid":
            from .retrieval import get_retriever
            retriever = get_retriever()
            # use_rerank=False：外层 HybridRetriever._rerank 是同步阻塞的 DashScope 调用，
            # 走同一个共享 Session（见 rerank-connection-reset 记忆），且 hybrid 的 RRF
            # 融合后 top-k 对表格库已足够，无需再精排。
            results = await retriever.search(query, top_k=top_k, mode="hybrid", use_rerank=False)
            # RRF 分数是倒数排名融合值（k=60 时 ~0.016~0.033），量纲与 dense 的
            # 降序伪分数（0.7-0.03i）不一致，直接展示会变成「相关度 0.02」误导模型。
            # 这里按名次重映射成与 dense 一致的降序伪分数，仅用于展示/引用，不改变排序。
            out = []
            for i, r in enumerate(results):
                d = r.to_dict()
                d["score"] = round(0.7 - i * 0.03, 3)
                out.append(d)
            return {
                "status": "success",
                "results": out,
                "count": len(out),
                "mode": "hybrid",
            }

        # dense mode: 使用结构化检索获取分块结果
        indexer = get_indexer()
        structured = await indexer._search_structured_async(query, top_k, use_rerank=use_rerank)
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
            "results": [{"content": _clean_context(context or ""), "score": 1.0}],
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
