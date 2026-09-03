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
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .progress import CURRENT_THREAD_ID  # 提交/查询时记录所在对话线程（结果回流）

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


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量，缺失/非法时回退默认值（带默认值的配置项约定）。"""
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """读取秒数环境变量（允许小数，便于测试用小超时）。非法时回退默认值。"""
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


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
        "main_thread_id": job.main_thread_id,
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
    main_thread_id: str | None = None,
) -> dict:
    """插入一个 queued 任务，返回 job 摘要（含 job_id）。

    main_thread_id：提交时所在的对话线程（结果回流用，可空）。
    """
    from api.models import RpaJob

    if job_type not in JOB_TOOL_MAP:
        raise ValueError(f"未知任务类型: {job_type}")
    job = RpaJob(
        job_id=_new_job_id(),
        job_type=job_type,
        params=json.dumps(params, ensure_ascii=False),
        status=STATUS_QUEUED,
        created_by=created_by,
        main_thread_id=main_thread_id,
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


async def get_latest_rpa_job_by_thread(session: AsyncSession, thread_id: str) -> dict | None:
    """按对话线程取最近提交的一条 RPA 任务（结果回流：无参 get_rpa_job_status 用）。"""
    from api.models import RpaJob

    stmt = (
        select(RpaJob)
        .where(RpaJob.main_thread_id == thread_id)
        .order_by(RpaJob.created_at.desc(), RpaJob.id.desc())
        .limit(1)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
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
    # 记录提交时所在的对话线程（结果回流：用户随后无参 get_rpa_job_status 命中本任务）。
    # CURRENT_THREAD_ID 由 chat._run_turn 在 agent.astream 前置入同一 asyncio context，
    # submit 工具经 await tool.ainvoke 同任务执行，contextvar 天然传播。
    thread_id = CURRENT_THREAD_ID.get() or None
    try:
        async with factory() as session:
            job = await create_rpa_job(session, job_type, params, main_thread_id=thread_id)
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


@tool(description="查询 RPA 任务最新状态与结果（结果回流对话）。job_id 留空时自动解析当前对话线程最近提交的 RPA 任务")
async def get_rpa_job_status(job_id: str = "") -> dict:
    """查询 RPA 任务状态与结果（只读、低风险、无需审批，viewer+ 可用）。

    供结果回流对话：提交 submit_rpa_* 后，用户问「任务完成了吗 / 结果如何」时，
    agent 用本工具拉取最新结果整理进回复。两种用法：
    - job_id 非空：精确查询该任务；
    - job_id 留空：解析「当前对话线程最近提交的 RPA 任务」——提交那一轮已把线程
      ID 记在 main_thread_id 上，直接无参调用即可命中。

    Args:
        job_id: RPA 任务 ID（submit_rpa_* 提交时返回，形如 rpa-xxxxxxxxxxxx）。
                留空 = 查询当前对话线程最近提交的任务。
    """
    factory = _get_session_factory()
    try:
        async with factory() as session:
            if job_id:
                job = await get_rpa_job(session, job_id)
            else:
                thread_id = CURRENT_THREAD_ID.get()
                if not thread_id:
                    return {
                        "status": "not_found",
                        "message": "未提供 job_id 且当前无对话线程上下文，无法定位任务；请传入 submit_rpa_* 返回的 job_id。",
                    }
                job = await get_latest_rpa_job_by_thread(session, thread_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询 RPA 任务状态失败")
        return {"status": "error", "message": f"查询任务状态失败: {exc}"}

    if job is None:
        return {
            "status": "not_found",
            "message": f"未找到 RPA 任务{' ' + job_id if job_id else '（当前对话线程还没有提交过任务）'}。",
        }
    result_txt = f"，结果：{job['result']}" if job.get("result") else ""
    error_txt = f"，错误：{job['error']}" if job.get("error") else ""
    return {
        **job,
        "message": f"RPA 任务 {job['job_id']}（{job['job_type']}）当前状态：{job['status']}{result_txt}{error_txt}",
    }


def get_rpa_query_tools() -> list:
    """RPA 结果查询工具（只读，挂 core tools，供结果回流对话）。"""
    return [get_rpa_job_status]


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


async def _sweep_stale_running(max_age_seconds: int | None = None) -> int:
    """僵死守护：把超过最大执行时长的 running 任务标记为 failed。

    在调度器 _loop 每轮认领前调用，防止 MCP 挂起/进程僵死把任务永远钉在
    running（面板一直"进行中"，只有重启才恢复）。max_age 读环境变量
    RPA_JOB_MAX_RUNTIME_SECONDS，默认 1800。

    策略：started_at 为 NULL 的 running 行**跳过不杀**——认领与写 started_at 在
    同一事务（claim_next_rpa_job），NULL running 只可能来自「认领瞬间被中断」的
    极小窗口或手工脏数据，误杀风险大于收益；真正的进程崩溃残留由启动时
    _requeue_stale_running()（at-least-once）处理。

    Returns:
        本次清扫命中（标记失败）的行数。
    """
    from api.models import RpaJob

    max_age = _env_int("RPA_JOB_MAX_RUNTIME_SECONDS", 1800) if max_age_seconds is None else max_age_seconds
    if max_age <= 0:
        return 0
    cutoff = _now_utc() - timedelta(seconds=max_age)
    factory = _get_session_factory()
    async with factory() as session:
        result = await session.execute(
            update(RpaJob)
            .where(RpaJob.status == STATUS_RUNNING)
            .where(RpaJob.started_at.is_not(None))
            .where(RpaJob.started_at < cutoff)
            .values(
                status=STATUS_FAILED,
                finished_at=_now_utc(),
                error=f"任务超过最大执行时长({max_age}s)自动标记失败",
            )
        )
        await session.commit()
        if result.rowcount:
            logger.warning("僵死清扫：%d 个 running 任务超 %ds，自动标记 failed", result.rowcount, max_age)
        return result.rowcount or 0


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
                # 先清扫僵死任务（running 超 RPA_JOB_MAX_RUNTIME_SECONDS 的标 failed），
                # 释放调度器单队列；再认领新任务。
                try:
                    await _sweep_stale_running()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("僵死清扫异常（可忽略）: %s", exc)
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

        timeout = _env_float("RPA_EXEC_TIMEOUT_SECONDS", 1200.0)
        from agent.mcp_setup import ensure_mcp_for_intent, _mcp_importers, evict_rpa

        # 连接 + 单次工具调用分别超时：RPA_MCP 挂起时若不掐断，会占死调度器单队列，
        # 后续真实任务全部排队停滞。默认 1200s（20 分钟），大于已知最长合法流程
        # （三类任务 5~15 分钟）留余量。
        try:
            await asyncio.wait_for(ensure_mcp_for_intent("rpa"), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"RPA MCP 连接超时(>{timeout:g}s)，任务已标记失败") from None
        importer = _mcp_importers.get("RPA")
        if importer is None:
            raise RuntimeError("RPA MCP executor 未连接，无法执行任务")
        tool_name = JOB_TOOL_MAP[job_type]
        try:
            result = await asyncio.wait_for(importer.call_tool(tool_name, params), timeout=timeout)
        except asyncio.TimeoutError:
            # 超时与「合法慢任务」在分钟级不可分辨：不自动杀 executor 进程（stdio
            # 可后续加"超时连带重启 importer 子进程"止损；HTTP 场景做不到）。只在
            # error 里提示去面板确认，避免把「已标 failed 但副作用已完成」误当成功。
            raise RuntimeError(
                f"RPA 执行超时(>{timeout:g}s)，任务已标记失败；"
                "请到「RPA 任务」面板确认该任务实际是否已执行，避免重复操作"
            ) from None
        except Exception:
            # 非超时的连接级故障（session 断开 / executor 崩溃）→ 驱逐 RPA 连接并
            # 进入退避窗口，下个任务到来时自动重建连接（mcp_setup.evict_rpa）。
            logger.warning("RPA MCP 调用异常，驱逐连接待重建: job_type=%s", job_type, exc_info=True)
            await evict_rpa()
            raise
        return _mcp_result_text(result, tool_name)


_dispatcher: RpaJobDispatcher | None = None


def get_rpa_dispatcher() -> RpaJobDispatcher:
    """获取调度器单例（线程安全，惰性创建）。"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = RpaJobDispatcher()
    return _dispatcher
