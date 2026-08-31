# -*- coding: utf-8 -*-
"""RPA 任务队列 — 提交工具 + 后台调度器（当前电脑单机实现）。

架构：**DB 当队列**（`rpa_jobs` 表，Postgres），后台调度器（本模块单例）
按序认领任务，经独立 RPA MCP server 执行（本机 stdio 子进程 / 跨机 HTTP），
一次一个。聊天回合只调 `submit_rpa_*` 工具（审批拦提交），立即返回 job_id，
任务在后台排队执行，回合秒完。

RPA 永远独立进程：本模块只经 `mcp_setup._mcp_importers["RPA"].call_tool()` 把
任务推给 executor，进程内不直接调用 `skills.rpa.tasks.*.run()`。

DRY_RUN：设置 `RPA_DRY_RUN=1` 时调度器跳过真实 MCP 调用、直接标记 done +
假结果 — 用于只读冒烟/演示（绝不触发真实业务）。
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── 任务状态 ──
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
JOB_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED)

# ── 任务类型 → 对应 RPA MCP 工具名 ──
# MCP 侧工具名不带 `mcp_` 前缀（见 skills.rpa.mcp_server 注册名）。
JOB_TOOL_MAP: dict[str, str] = {
    "query_campaign_spend": "rpa_query_campaign_spend",
    "collect_amazon_review": "rpa_collect_amazon_review",
    "update_track_table": "rpa_update_track_table",
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _new_job_id() -> str:
    return "rpa-" + uuid.uuid4().hex[:12]


# ── session 工厂（测试可替换为内存 sqlite sessionmaker）──
_session_factory = None  # type: ignore[var-annotated]


def _get_session_factory():
    """返回 async_sessionmaker。默认 = 项目 Postgres；测试注入内存 sqlite。"""
    global _session_factory
    if _session_factory is None:
        from api.database import async_session
        return async_session
    return _session_factory


# ── Job 仓库（接收 session，便于测试注入）──


def _job_to_dict(job) -> dict:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "params": job.params,
        "status": job.status,
        "error": job.error,
        "result": job.result,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


async def create_rpa_job(
    session: AsyncSession,
    job_type: str,
    params: dict,
    created_by: int | None = None,
) -> dict:
    """插入一个 queued 任务，返回 job 摘要（含 job_id）。"""
    from api.models import RpaJob

    if job_type not in JOB_TOOL_MAP:
        raise ValueError(f"未知任务类型: {job_type}")
    job = RpaJob(
        job_id=_new_job_id(),
        job_type=job_type,
        params=json.dumps(params, ensure_ascii=False),
        status=STATUS_QUEUED,
        created_by=created_by,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return _job_to_dict(job)


async def claim_next_rpa_job(session: AsyncSession):
    """认领最老的 queued 任务并置为 running（FOR UPDATE SKIP LOCKED，天然串行）。

    多台机器/多个调度器并发时不会重复取到同一任务。
    """
    from api.models import RpaJob

    stmt = (
        select(RpaJob)
        .where(RpaJob.status == STATUS_QUEUED)
        .order_by(RpaJob.created_at.asc(), RpaJob.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is not None:
        job.status = STATUS_RUNNING
        job.started_at = _now_utc()
        await session.commit()
        await session.refresh(job)
    return job


async def finish_rpa_job(
    session: AsyncSession,
    job_id: str,
    *,
    status: str,
    result: Any = None,
    error: str | None = None,
) -> None:
    """标记任务完成/失败并记录结果或错误。"""
    from api.models import RpaJob

    result_value = None
    if result is not None:
        result_value = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    stmt = (
        update(RpaJob)
        .where(RpaJob.job_id == job_id)
        .values(status=status, finished_at=_now_utc(), result=result_value, error=error)
    )
    await session.execute(stmt)
    await session.commit()


async def get_rpa_job(session: AsyncSession, job_id: str) -> dict | None:
    from api.models import RpaJob

    job = (await session.execute(select(RpaJob).where(RpaJob.job_id == job_id))).scalar_one_or_none()
    return _job_to_dict(job) if job is not None else None


async def list_rpa_jobs(
    session: AsyncSession,
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[dict]:
    from api.models import RpaJob

    stmt = select(RpaJob).order_by(RpaJob.created_at.desc(), RpaJob.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(RpaJob.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [_job_to_dict(j) for j in rows]


# ── submit 工具（审批拦提交，立即返回 job_id，不执行）──


async def _submit_job(job_type: str, params: dict) -> dict:
    factory = _get_session_factory()
    try:
        async with factory() as session:
            job = await create_rpa_job(session, job_type, params)
    except Exception as exc:  # noqa: BLE001
        logger.exception("RPA 任务提交失败: %s", job_type)
        return {"status": "error", "message": f"任务提交失败: {exc}"}
    return {
        "status": "submitted",
        "job_id": job["job_id"],
        "job_type": job_type,
        "message": (
            f"RPA 任务已提交，排队执行中（job_id={job['job_id']}）。"
            "任务由后台调度器依次执行，可在「RPA 任务」面板查看进度与结果。"
        ),
    }


# 注意：submit 工具的参数说明内联在 docstring 里，不 import skills.rpa 的
# manifest —— agent 进程刻意不加载 RPA 栈（skills/__init__.py 亦然），
# 保持「RPA 永远独立进程」。任务真正执行时由调度器把同名参数推给 RPA MCP。

@tool(description="提交亚马逊广告花费查询 RPA 任务（排队执行，立即返回 job_id，不阻塞对话）")
async def submit_rpa_query_campaign_spend(start_date: str, end_date: str = "", output_dir: str = "") -> dict:
    """提交亚马逊广告花费查询 RPA 任务（审批通过后排队执行，立即返回 job_id）。

    Args:
        start_date: 起始日期，格式 YYYY-MM-DD，如 2026-06-01。
        end_date: 结束日期，格式 YYYY-MM-DD。为空则只查 start_date 当天。
        output_dir: 广告花费报表输出目录。为空则用 .env 的 AD_SPEND_OUTPUT_DIR。
    """
    return await _submit_job("query_campaign_spend", {
        "start_date": start_date, "end_date": end_date, "output_dir": output_dir,
    })


@tool(description="提交 Amazon 评论数/星级采集 RPA 任务（排队执行，立即返回 job_id，不阻塞对话）")
async def submit_rpa_collect_amazon_review(excel_path: str = "") -> dict:
    """提交 Amazon 评论采集 RPA 任务（审批通过后排队执行，立即返回 job_id）。

    Args:
        excel_path: ASIN Excel 文件路径。为空则用 .env 的 AMAZON_REVIEW_EXCEL_PATH。
    """
    return await _submit_job("collect_amazon_review", {"excel_path": excel_path})


@tool(description="提交亚马逊轨迹跟踪表更新 RPA 任务（真实业务操作，需审批；排队执行，立即返回 job_id）")
async def submit_rpa_update_track_table(store: str = "") -> dict:
    """提交轨迹跟踪表更新 RPA 任务（审批通过后排队执行，立即返回 job_id）。

    Args:
        store: 店铺标签（如 '巧逗豆'/'天安'）。为空则处理 TRACK_TABLE_STORES 中全部店铺。
    """
    return await _submit_job("update_track_table", {"store": store})


def get_submit_rpa_tools() -> list:
    """提交侧 RPA 工具（挂在 core tools 上，LLM 只见 submit 不见 mcp_rpa_*）。"""
    return [
        submit_rpa_query_campaign_spend,
        submit_rpa_collect_amazon_review,
        submit_rpa_update_track_table,
    ]


# ── 后台调度器 ──


def _mcp_result_text(result: Any, tool_name: str) -> str:
    """把 MCP CallToolResult 转成文本（content 块 text 拼接，保留结构约定）。"""
    try:
        if result is None:
            return json.dumps({"status": "success", "message": f"{tool_name} 执行完成（无返回内容）"}, ensure_ascii=False)
        is_error = bool(getattr(result, "isError", False))
        content = getattr(result, "content", None) or []
        parts = []
        for c in content:
            if c is None:
                continue
            parts.append(getattr(c, "text", None) or str(c))
        text = "\n".join(str(p) for p in parts)
        if is_error:
            return json.dumps({"status": "error", "message": text or f"{tool_name} 执行返回错误"}, ensure_ascii=False)
        return text or json.dumps({"status": "success", "message": f"{tool_name} 执行完成（无内容）"}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"status": "error", "message": f"解析 MCP 结果失败: {exc}"}, ensure_ascii=False)


async def _requeue_stale_running() -> None:
    """服务重启后把上次残留的 running 任务重新排队（at-least-once）。

    上一轮执行被中断（进程被杀/断网），无法确认是否完成；重新执行是任务队列
    的标准语义，日志会记录受影响数量。
    """
    from api.models import RpaJob

    factory = _get_session_factory()
    async with factory() as session:
        result = await session.execute(
            update(RpaJob).where(RpaJob.status == STATUS_RUNNING).values(status=STATUS_QUEUED)
        )
        await session.commit()
        if result.rowcount:
            logger.info("重置 %d 个残留 running 任务为 queued", result.rowcount)


class RpaJobDispatcher:
    """后台调度器：每 2s 认领一个 queued 任务，经 RPA MCP executor 执行，一次一个。"""

    _POLL_INTERVAL = 2.0

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        try:
            await _requeue_stale_running()
        except Exception as exc:  # noqa: BLE001
            logger.warning("重置残留 running 任务失败（可忽略）: %s", exc)
        self._task = asyncio.create_task(self._loop(), name="rpa-dispatcher")
        logger.info("RPA 调度器已启动")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except BaseException:
            pass
        self._task = None
        logger.info("RPA 调度器已停止")

    async def _loop(self) -> None:
        while True:
            try:
                await self._dispatch_next()
            except asyncio.CancelledError:
                break
            except BaseException as exc:  # noqa: BLE001
                logger.exception("RPA 调度器循环异常: %s", exc)
            await asyncio.sleep(self._POLL_INTERVAL)

    async def _dispatch_next(self) -> None:
        factory = _get_session_factory()
        async with factory() as session:
            job = await claim_next_rpa_job(session)
        if job is None:
            return
        job_id = job.job_id
        logger.info("RPA 调度器认领任务 %s (%s)", job_id, job.job_type)
        try:
            result_text = await self._run_on_executor(job.job_type, json.loads(job.params or "{}"))
            async with factory() as session:
                await finish_rpa_job(session, job_id, status=STATUS_DONE, result=result_text)
            logger.info("RPA 任务完成 %s", job_id)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001
            logger.exception("RPA 任务执行失败 %s", job_id)
            async with factory() as session:
                await finish_rpa_job(session, job_id, status=STATUS_FAILED, error=str(exc)[:2000])

    async def _run_on_executor(self, job_type: str, params: dict) -> str:
        """把任务推给独立 RPA MCP executor；DRY_RUN 时跳过真实执行。"""
        if os.getenv("RPA_DRY_RUN", "").lower() in ("1", "true", "yes"):
            return json.dumps({
                "status": "success", "dry_run": True, "job_type": job_type, "params": params,
            }, ensure_ascii=False)

        from agent.mcp_setup import ensure_mcp_for_intent, _mcp_importers

        await ensure_mcp_for_intent("rpa")
        importer = _mcp_importers.get("RPA")
        if importer is None:
            raise RuntimeError("RPA MCP executor 未连接，无法执行任务")
        tool_name = JOB_TOOL_MAP[job_type]
        result = await importer.call_tool(tool_name, params)
        return _mcp_result_text(result, tool_name)


_dispatcher: RpaJobDispatcher | None = None


def get_rpa_dispatcher() -> RpaJobDispatcher:
    """获取调度器单例（线程安全，惰性创建）。"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = RpaJobDispatcher()
    return _dispatcher
