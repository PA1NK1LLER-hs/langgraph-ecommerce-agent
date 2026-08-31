"""知识库路由 — 搜索 + 文档上传/管理。

上传流程:
  1. 接收文件 → 临时存储
  2. 后台任务: 解析 → 分块 → embedding → Qdrant + Neo4j
  3. SSE 推送进度（前端轮询或 EventSource）
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from api.models import User as UserModel
from api.deps import get_current_user, require_editor
from rag import async_search_knowledge, async_list_sources

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb", tags=["knowledge"])

# 上传文件存储目录
UPLOAD_DIR = Path(".data/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 上传限制
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB

# SSRF 防护：阻止的 hostname / scheme
_BLOCKED_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1", "0", "[::1]"})
_BLOCKED_SCHEMES = frozenset({"file", "ftp", "gopher", "dict", "ldap", "sftp"})


def _validate_url(url_str: str) -> None:
    """SSRF 防护：校验 URL 仅允许 http/https 且不指向内网地址。"""
    try:
        parsed = urlparse(url_str)
    except Exception:
        raise HTTPException(status_code=400, detail="URL 格式无效")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="仅支持 http/https 协议")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="URL 缺少有效主机名")

    if hostname in _BLOCKED_HOSTS:
        raise HTTPException(status_code=400, detail="不允许访问内网地址")

    # 检查是否为私有/保留 IP
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            raise HTTPException(status_code=400, detail="不允许访问内网地址")
    except ValueError:
        pass  # 不是 IP 地址（域名），放行


# 索引进度追踪（内存，单进程）
_index_progress: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: str = Field(default="dense", pattern="^(dense|hybrid)$")


class URLImportRequest(BaseModel):
    url: str = Field(..., min_length=1)
    tags: str = Field(default="")


# ---------------------------------------------------------------------------
# 查询端点（原有）
# ---------------------------------------------------------------------------


@router.get("/stats")
async def api_kb_stats(
    user: Annotated[UserModel, Depends(get_current_user)],
):
    """获取知识库统计信息（需登录）。"""
    try:
        sources = await async_list_sources()
        return {
            "status": "success",
            "sources": sources.get("sources", []),
            "total_chunks": sources.get("total_chunks", 0),
        }
    except Exception as exc:
        logger.error("KB stats failed: %s", exc)
        raise HTTPException(status_code=500, detail="获取知识库统计失败，请稍后重试")


@router.post("/search")
async def api_kb_search(
    req: SearchRequest,
    user: Annotated[UserModel, Depends(get_current_user)],
):
    """语义/混合搜索知识库（需登录）。"""
    try:
        return await async_search_knowledge(req.query, top_k=req.top_k, mode=req.mode)
    except Exception as exc:
        logger.error("KB search failed: %s", exc)
        raise HTTPException(status_code=500, detail="搜索失败，请稍后重试")


# ---------------------------------------------------------------------------
# 文档上传
# ---------------------------------------------------------------------------


@router.post("/upload")
async def api_kb_upload(
    file: UploadFile = File(...),
    tags: str = Query(default=""),
    chunk_strategy: str = Query(default="semantic", pattern="^(semantic|fixed|recursive)$"),
    _user: Annotated[UserModel, Depends(require_editor)] = None,
):
    """上传文档到知识库。

    支持格式: PDF, DOCX, XLSX, PPTX, TXT, CSV, HTML, MD, JSON, XML, 图片。
    文件会被自动解析、分块、embedding 后存入 Qdrant + Neo4j 知识图谱。

    Args:
        file: 上传文件。
        tags: 逗号分隔的标签（可选）。
        chunk_strategy: 分块策略 — semantic / fixed / recursive。
    """
    # 1. 保存上传文件（限制大小）
    ext = Path(file.filename or "upload").suffix.lower()
    safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = UPLOAD_DIR / safe_name

    try:
        content = await file.read()
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大：{len(content) / 1024 / 1024:.1f}MB，上限 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB",
            )
        dest.write_bytes(content)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文件保存失败: {exc}")

    # 2. 解析 + 分块 + 索引
    task_id = uuid.uuid4().hex[:8]
    _index_progress[task_id] = {"status": "parsing", "file": file.filename, "progress": 0}

    try:
        from rag.parsers import parse_file
        from rag.indexer import async_index_knowledge

        # 解析文档（to_thread 避免阻塞事件循环）
        _index_progress[task_id].update({"status": "parsing", "progress": 10})
        parsed_doc, chunks = await asyncio.to_thread(
            parse_file, str(dest), chunk_strategy=chunk_strategy
        )
        # 图片文档：用视觉模型生成描述替换占位符分块，让检索能命中图片内容
        from agent.vision import enrich_image_chunks
        chunks = await enrich_image_chunks(parsed_doc, chunks)
        total_chunks = len(chunks)

        _index_progress[task_id].update({"status": "indexing", "progress": 20, "total_chunks": total_chunks})

        # 逐块索引（带进度）
        source = file.filename or f"upload-{task_id}"
        indexed = 0
        for i, chunk in enumerate(chunks):
            try:
                chunk_text = chunk.text if hasattr(chunk, "text") else str(chunk)
                await async_index_knowledge(chunk_text, source=source, tags=tags)
                indexed += 1
                pct = 20 + int((i + 1) / total_chunks * 75) if total_chunks > 0 else 95
                _index_progress[task_id].update({"progress": pct, "indexed": indexed})
            except Exception as exc:
                logger.warning("Failed to index chunk %d/%d: %s", i + 1, total_chunks, exc)

        _index_progress[task_id].update({"status": "done", "progress": 100, "indexed": indexed})

        # 清理临时文件
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass

        return {
            "status": "success",
            "task_id": task_id,
            "file": file.filename,
            "source": source,
            "total_chunks": total_chunks,
            "indexed_chunks": indexed,
        }

    except Exception as exc:
        _index_progress[task_id].update({"status": "error", "error": str(exc)})
        logger.error("Upload indexing failed for '%s': %s", file.filename, exc)
        raise HTTPException(status_code=500, detail="文档索引失败，请稍后重试")


@router.get("/upload-progress/{task_id}")
async def api_upload_progress(task_id: str):
    """查询索引进度。"""
    progress = _index_progress.get(task_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {"status": "success", "progress": progress}


# ---------------------------------------------------------------------------
# URL 导入
# ---------------------------------------------------------------------------


@router.post("/import-url")
async def api_import_url(
    req: URLImportRequest,
    _user: Annotated[UserModel, Depends(require_editor)] = None,
):
    """从 URL 导入网页到知识库。

    自动抓取网页、提取正文、分块、索引。
    """
    task_id = uuid.uuid4().hex[:8]
    _index_progress[task_id] = {"status": "fetching", "url": req.url, "progress": 0}

    # SSRF 防护：校验 URL
    _validate_url(req.url)

    try:
        from rag.parsers import parse_url
        from rag.indexer import async_index_knowledge

        _index_progress[task_id].update({"status": "parsing", "progress": 20})
        parsed_doc, chunks = await asyncio.to_thread(
            parse_url, req.url, chunk_strategy="semantic"
        )
        total_chunks = len(chunks)
        source = parsed_doc.metadata.get("source", req.url)

        _index_progress[task_id].update({"status": "indexing", "progress": 30, "total_chunks": total_chunks})

        indexed = 0
        for i, chunk in enumerate(chunks):
            try:
                chunk_text = chunk.text if hasattr(chunk, "text") else str(chunk)
                await async_index_knowledge(chunk_text, source=source, tags=req.tags)
                indexed += 1
                pct = 30 + int((i + 1) / total_chunks * 65) if total_chunks > 0 else 95
                _index_progress[task_id].update({"progress": pct, "indexed": indexed})
            except Exception as exc:
                logger.warning("Failed to index chunk %d/%d from URL: %s", i + 1, total_chunks, exc)

        _index_progress[task_id].update({"status": "done", "progress": 100, "indexed": indexed})

        return {
            "status": "success",
            "task_id": task_id,
            "url": req.url,
            "source": source,
            "total_chunks": total_chunks,
            "indexed_chunks": indexed,
        }

    except Exception as exc:
        _index_progress[task_id].update({"status": "error", "error": str(exc)})
        logger.error("URL import failed for '%s': %s", req.url, exc)
        raise HTTPException(status_code=500, detail="URL 导入失败，请稍后重试")


# ---------------------------------------------------------------------------
# 文档管理
# ---------------------------------------------------------------------------


@router.get("/sources/{source_id}/chunks")
async def api_get_source_chunks(
    source_id: str,
    limit: int = Query(default=50, ge=1, le=200),
):
    """获取指定来源的分块列表。

    Args:
        source_id: 来源标识（文件名/URL）。
        limit: 返回条数上限。
    """
    try:
        from rag.indexer import get_indexer
        # 返回该来源的真实分块（按 chunk_order_index 排序），而非语义搜索的近似结果
        chunks = await get_indexer()._list_source_chunks_async(source_id)
        # 截取前 limit 条（真实分块已按顺序排列，无需 top_k 语义截断）
        chunks = chunks[:limit]
        return {
            "status": "success",
            "source": source_id,
            "chunks": chunks,
            "count": len(chunks),
        }
    except Exception as exc:
        logger.error("KB get chunks failed for source '%s': %s", source_id, exc)
        raise HTTPException(status_code=500, detail="获取分块信息失败，请稍后重试")


@router.delete("/sources/{source_id}")
async def api_delete_source(
    source_id: str,
    _user: Annotated[UserModel, Depends(require_editor)] = None,
):
    """删除指定来源的所有文档（按 source/file_path 匹配 doc_id 逐个删除）。

    LightRAG 的 adelete_by_doc_id 会级联清理该文档的分块、实体与关系
    （仅属于该文档的图元素被移除，共享的用剩余文档重建）。
    """
    try:
        from rag.indexer import get_indexer

        indexer = get_indexer()
        deleted = await indexer.delete_source_async(source_id)
        if deleted == 0:
            return {
                "status": "not_found",
                "deleted": source_id,
                "message": "未找到该来源的已索引文档",
            }
        return {"status": "success", "deleted": source_id, "docs_removed": deleted}
    except Exception as exc:
        logger.error("KB delete source '%s' failed: %s", source_id, exc)
        raise HTTPException(status_code=500, detail="删除来源失败，请稍后重试")


@router.post("/reindex")
async def api_reindex(
    file: UploadFile = File(...),
    source: str = Form(default=""),
    _user: Annotated[UserModel, Depends(require_editor)] = None,
):
    """更新知识库文档：先删除该来源的旧索引，再用新上传的文件重新解析索引。

    对应关系/内容发生变化的文档应通过此端点更新——直接重复上传只会
    增量叠加，旧实体关系不会被覆盖。

    Args:
        file: 新版文档文件。
        source: 来源名（缺省使用文件名，与 upload 端点行为一致）。
    """
    source_name = source.strip() or (file.filename or "")
    if not source_name:
        raise HTTPException(status_code=400, detail="缺少来源名称")

    task_id = uuid.uuid4().hex[:8]
    _index_progress[task_id] = {"status": "starting", "source": source_name, "progress": 0}

    try:
        from rag.indexer import get_indexer

        # 1. 真正删除旧索引（LightRAG 级联清理分块与仅属于该文档的实体/关系）
        deleted_old = await get_indexer().delete_source_async(source_name)
        logger.info("Reindex '%s': removed %d old doc(s)", source_name, deleted_old)
        _index_progress[task_id].update(
            {"status": "deleting_old", "progress": 5, "removed_old": deleted_old}
        )

        # 2. 保存上传的新文件
        ext = Path(file.filename or "upload").suffix.lower()
        safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
        dest = UPLOAD_DIR / safe_name
        content = await file.read()
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"文件过大：{len(content) / 1024 / 1024:.1f}MB，上限 {MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB",
            )
        dest.write_bytes(content)

        # 3. 解析 + 分块 + 索引
        from rag.parsers import parse_file
        from rag.indexer import async_index_knowledge

        _index_progress[task_id].update({"status": "parsing", "progress": 10})
        parsed_doc, chunks = await asyncio.to_thread(parse_file, str(dest), chunk_strategy="semantic")
        # 图片文档：用视觉模型生成描述替换占位符分块，让检索能命中图片内容
        from agent.vision import enrich_image_chunks
        chunks = await enrich_image_chunks(parsed_doc, chunks)
        total = len(chunks)

        indexed = 0
        for i, chunk in enumerate(chunks):
            try:
                chunk_text = chunk.text if hasattr(chunk, "text") else str(chunk)
                await async_index_knowledge(chunk_text, source=source_name)
                indexed += 1
                pct = 10 + int((i + 1) / total * 85) if total > 0 else 95
                _index_progress[task_id].update({"progress": pct, "indexed": indexed})
            except Exception as exc:
                logger.warning("Reindex chunk %d/%d failed: %s", i + 1, total, exc)

        # 清理临时文件
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass

        _index_progress[task_id].update({"status": "done", "progress": 100, "indexed": indexed})

        return {
            "status": "success",
            "task_id": task_id,
            "source": source_name,
            "removed_old_docs": deleted_old,
            "total_chunks": total,
            "indexed_chunks": indexed,
        }

    except HTTPException:
        raise
    except Exception as exc:
        _index_progress[task_id].update({"status": "error", "error": str(exc)})
        logger.error("Reindex failed for '%s': %s", source_name, exc)
        raise HTTPException(status_code=500, detail="重新索引失败，请稍后重试")
