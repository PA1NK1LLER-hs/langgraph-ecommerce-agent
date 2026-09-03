# -*- coding: utf-8 -*-
"""RPA 生产可靠性修复 — 单测（内存 sqlite + DRY_RUN / 假 importer）。

覆盖（对应修订版提示词任务一/二/三）：
- 任务一：MCP 调用/连接超时 → 任务 FAILED + error 含超时信息；僵死清扫
  （_sweep_stale_running）只杀超旧的 running，保留 fresh / started_at=NULL；
- 任务二：mcp_setup 失败退避决策、evict_rpa() 驱逐 RPA 连接并进入退避、调度器
  非超时调用异常时驱逐并重抛 → 任务 FAILED；
- 任务三：main_thread_id 落库与读取、CURRENT_THREAD_ID 提交时记录、无参
  get_rpa_job_status 按线程解析最近任务。

不连 Postgres：全部走注入的内存 aiosqlite session 工厂。对 agent.mcp_setup 的
全局状态（_failed_at/_connected/_mcp_importers）做快照还原，避免污染其它用例。
"""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from agent import rpa_jobs
import agent.mcp_setup as mcp_setup
from agent.progress import CURRENT_THREAD_ID
from agent.rpa_jobs import (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_FAILED,
    create_rpa_job,
    claim_next_rpa_job,
    finish_rpa_job,
    get_rpa_job,
    get_rpa_dispatcher,
    get_rpa_job_status,
    submit_rpa_query_campaign_spend,
    submit_rpa_collect_amazon_review,
    _sweep_stale_running,
    _now_utc,
)
from api.models import RpaJob


# ── 内存 sqlite 夹具（与 test_rpa_jobs.py 同一模式）──


@pytest.fixture
def session_factory():
    from api.database import Base
    import api.models  # noqa: F401 — 确保 User/Thread/RpaJob 注册进 Base.metadata

    async def _build():
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine, factory

    engine, factory = asyncio.run(_build())
    yield factory
    asyncio.run(engine.dispose())


@pytest.fixture
def use_session_factory(session_factory):
    old = rpa_jobs._session_factory
    rpa_jobs._session_factory = session_factory
    yield session_factory
    rpa_jobs._session_factory = old


@pytest.fixture(autouse=True)
def _isolate_mcp_state():
    """快照/还原 mcp_setup 全局连接状态，防止本模块用例互相污染。"""
    saved_failed = dict(mcp_setup._failed_at)
    saved_connected = set(mcp_setup._connected)
    saved_importers = dict(mcp_setup._mcp_importers)
    yield
    mcp_setup._failed_at.clear()
    mcp_setup._failed_at.update(saved_failed)
    mcp_setup._connected.clear()
    mcp_setup._connected.update(saved_connected)
    mcp_setup._mcp_importers.clear()
    mcp_setup._mcp_importers.update(saved_importers)


# ═══════════════════════════════════════════════════
# 任务一 · 4.1 MCP 调用/连接超时
# ═══════════════════════════════════════════════════


class TestExecTimeout:
    @pytest.mark.asyncio
    async def test_call_timeout_marks_failed(self, use_session_factory, monkeypatch):
        """call_tool 睡死 → 任务 FAILED + error 含超时提示。"""
        monkeypatch.setenv("RPA_EXEC_TIMEOUT_SECONDS", "0.05")
        monkeypatch.delenv("RPA_DRY_RUN", raising=False)
        async def _noop_ensure(intent):
            return None
        monkeypatch.setattr(mcp_setup, "ensure_mcp_for_intent", _noop_ensure)

        class HangingImporter:
            async def call_tool(self, name, arguments):
                await asyncio.sleep(999)

        monkeypatch.setattr(mcp_setup, "_mcp_importers", {"RPA": HangingImporter()})
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})
        await get_rpa_dispatcher()._dispatch_next()
        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
        assert got["status"] == STATUS_FAILED
        assert "超时" in got["error"]

    @pytest.mark.asyncio
    async def test_connect_timeout_marks_failed(self, use_session_factory, monkeypatch):
        """ensure_mcp 挂起 → 任务 FAILED + error 含连接超时。"""
        monkeypatch.setenv("RPA_EXEC_TIMEOUT_SECONDS", "0.05")
        monkeypatch.delenv("RPA_DRY_RUN", raising=False)
        async def _hang_ensure(intent):
            await asyncio.sleep(999)
        monkeypatch.setattr(mcp_setup, "ensure_mcp_for_intent", _hang_ensure)
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})
        await get_rpa_dispatcher()._dispatch_next()
        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
        assert got["status"] == STATUS_FAILED
        assert "连接超时" in got["error"]


# ═══════════════════════════════════════════════════
# 任务一 · 4.2 僵死任务清扫
# ═══════════════════════════════════════════════════


class TestSweepStaleRunning:
    @pytest.mark.asyncio
    async def test_sweep_marks_stale_failed(self, use_session_factory):
        """started_at 超旧（1h 前）的 running 任务被清扫为 FAILED。"""
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})
            await claim_next_rpa_job(session)  # → running, started_at=now
            old = _now_utc() - timedelta(hours=1)
            await session.execute(
                update(RpaJob).where(RpaJob.job_id == job["job_id"]).values(started_at=old)
            )
            await session.commit()
        n = await _sweep_stale_running(max_age_seconds=300)
        assert n == 1
        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
        assert got["status"] == STATUS_FAILED
        assert "最大执行时长" in got["error"]

    @pytest.mark.asyncio
    async def test_sweep_keeps_fresh_and_null_started(self, use_session_factory):
        """fresh running 与 started_at=NULL 的 running 都不杀（策略见 _sweep 注释）。"""
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            fresh = await create_rpa_job(session, "collect_amazon_review", {})
            await claim_next_rpa_job(session)  # running, started_at=now（新鲜）
            nulled = await create_rpa_job(session, "query_campaign_spend", {})
            await claim_next_rpa_job(session)
            await session.execute(
                update(RpaJob).where(RpaJob.job_id == nulled["job_id"]).values(started_at=None)
            )
            await session.commit()
        n = await _sweep_stale_running(max_age_seconds=300)
        assert n == 0
        async with factory() as session:
            got_fresh = await get_rpa_job(session, fresh["job_id"])
            got_null = await get_rpa_job(session, nulled["job_id"])
        assert got_fresh["status"] == STATUS_RUNNING
        assert got_null["status"] == STATUS_RUNNING

    @pytest.mark.asyncio
    async def test_sweep_env_default_threshold(self, use_session_factory, monkeypatch):
        """max_age 缺省读 RPA_JOB_MAX_RUNTIME_SECONDS（默认 1800）。"""
        monkeypatch.setenv("RPA_JOB_MAX_RUNTIME_SECONDS", "0")  # 关闭清扫
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})
            await claim_next_rpa_job(session)
        n = await _sweep_stale_running()
        assert n == 0  # max_age=0 → 不清扫


# ═══════════════════════════════════════════════════
# 任务二 · mcp_setup 退避重连 + 驱逐（仅 RPA）
# ═══════════════════════════════════════════════════


class TestBackoffDecision:
    def test_within_backoff_window(self, monkeypatch):
        monkeypatch.setenv("RPA_MCP_RECONNECT_BACKOFF_SECONDS", "60")
        now = mcp_setup._mono()
        mcp_setup._failed_at["RPA"] = now
        assert mcp_setup._within_backoff("RPA", now=now + 10) is True
        assert mcp_setup._within_backoff("RPA", now=now + 59) is True
        assert mcp_setup._within_backoff("RPA", now=now + 61) is False  # 窗口外放行
        assert mcp_setup._within_backoff("Time") is False  # 未失败过

    def test_clear_connect_failed(self):
        mcp_setup._failed_at["RPA"] = mcp_setup._mono()
        mcp_setup._clear_connect_failed("RPA")
        assert "RPA" not in mcp_setup._failed_at

    @pytest.mark.asyncio
    async def test_connect_skipped_within_backoff(self, monkeypatch):
        """退避窗口内不实例化 importer（不拉起子进程），直接返回 0。"""
        monkeypatch.setenv("RPA_MCP_RECONNECT_BACKOFF_SECONDS", "60")
        mcp_setup._failed_at["RPA"] = mcp_setup._mono()
        constructed = []
        monkeypatch.setattr(
            mcp_setup, "MCPToolImporter",
            lambda *a, **k: constructed.append(1) or object(),
        )
        n = await mcp_setup._connect_and_register("RPA", url="http://x", register=False)
        assert n == 0
        assert constructed == []  # 未拉起连接

    @pytest.mark.asyncio
    async def test_connect_outside_backoff_attempts_and_refails(self, monkeypatch):
        """窗口外重试：尝试连接，失败后重新记录失败时刻（回到窗口内）。"""
        monkeypatch.setenv("RPA_MCP_RECONNECT_BACKOFF_SECONDS", "60")
        mcp_setup._failed_at["RPA"] = mcp_setup._mono() - 100  # 窗口外

        class FailingImporter:
            def __init__(self, config):
                self.config = config

            async def connect(self):
                raise RuntimeError("boom")

            async def import_tools(self):
                return []

        monkeypatch.setattr(mcp_setup, "MCPToolImporter", FailingImporter)
        n = await mcp_setup._connect_and_register("RPA", url="http://x", register=False)
        assert n == 0
        assert mcp_setup._within_backoff("RPA") is True  # 失败重新计时


class TestEvictRpa:
    @pytest.mark.asyncio
    async def test_evict_clears_connection_and_enters_backoff(self, monkeypatch):
        monkeypatch.setenv("RPA_MCP_RECONNECT_BACKOFF_SECONDS", "60")
        disconnected = []

        class FakeImporter:
            async def disconnect(self):
                disconnected.append(True)

        mcp_setup._connected.add("RPA")
        mcp_setup._mcp_importers["RPA"] = FakeImporter()
        # 无关服务不受驱逐影响
        mcp_setup._connected.add("Filesystem")
        mcp_setup._mcp_importers["Filesystem"] = object()

        await mcp_setup.evict_rpa()
        assert "RPA" not in mcp_setup._connected
        assert "RPA" not in mcp_setup._mcp_importers
        assert disconnected == [True]
        assert mcp_setup._within_backoff("RPA") is True  # 进入退避
        assert "Filesystem" in mcp_setup._connected  # register=True 服务不驱逐

    @pytest.mark.asyncio
    async def test_dispatch_call_error_evicts_and_marks_failed(self, use_session_factory, monkeypatch):
        """调度器 call_tool 抛连接级异常 → evict_rpa() 被调 + 任务 FAILED + 错误保留。"""
        monkeypatch.delenv("RPA_DRY_RUN", raising=False)
        async def _noop_ensure(intent):
            return None
        monkeypatch.setattr(mcp_setup, "ensure_mcp_for_intent", _noop_ensure)
        evicted = []

        async def _fake_evict():
            evicted.append(True)
        monkeypatch.setattr(mcp_setup, "evict_rpa", _fake_evict)

        class BrokenImporter:
            async def call_tool(self, name, arguments):
                raise RuntimeError("session closed")

        monkeypatch.setattr(mcp_setup, "_mcp_importers", {"RPA": BrokenImporter()})
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})
        await get_rpa_dispatcher()._dispatch_next()
        assert evicted == [True]
        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
        assert got["status"] == STATUS_FAILED
        assert "session closed" in got["error"]


# ═══════════════════════════════════════════════════
# 任务三 · 结果回流（main_thread_id + get_rpa_job_status）
# ═══════════════════════════════════════════════════


class TestResultBackflow:
    @pytest.mark.asyncio
    async def test_create_job_roundtrips_thread(self, use_session_factory):
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "update_track_table", {}, main_thread_id="thread-1")
            assert job["main_thread_id"] == "thread-1"
            got = await get_rpa_job(session, job["job_id"])
        assert got["main_thread_id"] == "thread-1"

    @pytest.mark.asyncio
    async def test_submit_records_current_thread(self, use_session_factory):
        """CURRENT_THREAD_ID 已设时，submit 落库正确 main_thread_id。"""
        factory = rpa_jobs._get_session_factory()
        token = CURRENT_THREAD_ID.set("thread-abc")
        try:
            res = await submit_rpa_query_campaign_spend.ainvoke(
                {"start_date": "2026-06-01", "end_date": "2026-06-30", "output_dir": ""}
            )
        finally:
            CURRENT_THREAD_ID.reset(token)
        assert res["status"] == "submitted"
        async with factory() as session:
            got = await get_rpa_job(session, res["job_id"])
        assert got["main_thread_id"] == "thread-abc"

    @pytest.mark.asyncio
    async def test_submit_no_thread_is_null(self, use_session_factory):
        """无线程上下文（默认空）时 main_thread_id 为 NULL。"""
        factory = rpa_jobs._get_session_factory()
        token = CURRENT_THREAD_ID.set("")
        try:
            res = await submit_rpa_collect_amazon_review.ainvoke({"excel_path": ""})
        finally:
            CURRENT_THREAD_ID.reset(token)
        assert res["status"] == "submitted"
        async with factory() as session:
            got = await get_rpa_job(session, res["job_id"])
        assert got["main_thread_id"] is None

    @pytest.mark.asyncio
    async def test_status_by_job_id(self, use_session_factory):
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})
            await finish_rpa_job(session, job["job_id"], status=STATUS_DONE, result={"rows": 3})
        res = await get_rpa_job_status.ainvoke({"job_id": job["job_id"]})
        assert res["status"] == STATUS_DONE
        assert res["job_id"] == job["job_id"]
        assert "rows" in res["result"]

    @pytest.mark.asyncio
    async def test_status_no_id_resolves_thread_latest(self, use_session_factory):
        """无参 get_rpa_job_status 按 CURRENT_THREAD_ID 命中本线程最近任务。"""
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {}, main_thread_id="t1")
            await finish_rpa_job(session, job["job_id"], status=STATUS_DONE, result={"ok": 1})
        token = CURRENT_THREAD_ID.set("t1")
        try:
            res = await get_rpa_job_status.ainvoke({})
        finally:
            CURRENT_THREAD_ID.reset(token)
        assert res["status"] == STATUS_DONE
        assert res["job_id"] == job["job_id"]

    @pytest.mark.asyncio
    async def test_status_no_thread_no_id_returns_notfound(self, use_session_factory):
        token = CURRENT_THREAD_ID.set("")
        try:
            res = await get_rpa_job_status.ainvoke({})
        finally:
            CURRENT_THREAD_ID.reset(token)
        assert res["status"] == "not_found"
        assert "job_id" in res["message"] or "线程" in res["message"]

    @pytest.mark.asyncio
    async def test_status_unknown_id_notfound(self, use_session_factory):
        res = await get_rpa_job_status.ainvoke({"job_id": "rpa-no-such-job"})
        assert res["status"] == "not_found"
        assert "rpa-no-such-job" in res["message"]
