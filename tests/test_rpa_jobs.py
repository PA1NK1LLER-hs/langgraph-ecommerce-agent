# -*- coding: utf-8 -*-
"""RPA 任务队列 — 仓库函数 + submit 工具 + 调度器（内存 sqlite / DRY_RUN）。

不连 Postgres：全部走注入的内存 aiosqlite session 工厂；调度器执行走
RPA_DRY_RUN=1 分支（跳过真实 MCP 调用，绝不触真实业务）。

异步约定：pytest-asyncio strict 模式，async 测试显式 @pytest.mark.asyncio
（与 tests/test_integration.py 一致）。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from agent import rpa_jobs
from agent.rpa_jobs import (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_DONE,
    STATUS_FAILED,
    JOB_STATUSES,
    JOB_TOOL_MAP,
    create_rpa_job,
    claim_next_rpa_job,
    finish_rpa_job,
    get_rpa_job,
    list_rpa_jobs,
    _requeue_stale_running,
    _mcp_result_text,
    get_rpa_dispatcher,
    submit_rpa_query_campaign_spend,
    submit_rpa_collect_amazon_review,
    submit_rpa_update_track_table,
)


@pytest.fixture
def session_factory():
    """每个测试一个全新的内存 sqlite 库（含全部表）。

    aiosqlite 每个连接跑在独立线程事件循环里，引擎可跨测试 loop 使用，
    因此夹具内部用 asyncio.run() 建表/销毁即可（Python 3.14 无隐式 loop）。
    """
    from api.database import Base
    import api.models  # noqa: F401  — 确保 User/Thread/RpaJob 注册进 Base.metadata

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
    """把模块级 session 工厂替换为内存库，测试后还原。"""
    old = rpa_jobs._session_factory
    rpa_jobs._session_factory = session_factory
    yield session_factory
    rpa_jobs._session_factory = old


# ═══════════════════════════════════════════════════
# 常量映射
# ═══════════════════════════════════════════════════


class TestConstants:
    def test_job_statuses(self):
        assert JOB_STATUSES == (STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED)

    def test_job_tool_map(self):
        assert JOB_TOOL_MAP == {
            "query_campaign_spend": "rpa_query_campaign_spend",
            "collect_amazon_review": "rpa_collect_amazon_review",
            "update_track_table": "rpa_update_track_table",
        }

    def test_rpa_tool_prefixes_include_submit(self):
        """submit_rpa_* 命中 _RPA_TOOL_PREFIXES：提交后跨轮保留 RPA 上下文。"""
        from agent.graph import _RPA_TOOL_PREFIXES
        assert "submit_rpa_" in _RPA_TOOL_PREFIXES
        assert "mcp_rpa_" in _RPA_TOOL_PREFIXES

    def test_dispatcher_singleton(self):
        assert get_rpa_dispatcher() is get_rpa_dispatcher()


# ═══════════════════════════════════════════════════
# Job 仓库
# ═══════════════════════════════════════════════════


class TestJobRepo:
    @pytest.mark.asyncio
    async def test_create_job(self, use_session_factory):
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "update_track_table", {"store": "巧逗豆"})
            assert job["status"] == STATUS_QUEUED
            assert job["job_type"] == "update_track_table"
            assert job["job_id"].startswith("rpa-")
            assert job["created_at"]  # server_default 已回填

        # 持久化可查
        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
            assert got is not None
            assert got["params"] == json.dumps({"store": "巧逗豆"}, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_create_unknown_type_raises(self, use_session_factory):
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            with pytest.raises(ValueError):
                await create_rpa_job(session, "not_a_real_task", {})

    @pytest.mark.asyncio
    async def test_claim_serial(self, use_session_factory):
        """认领按创建先后，一次一个；无 queued 时返回 None。"""
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            a = await create_rpa_job(session, "query_campaign_spend", {"start_date": "2026-06-01"})
            b = await create_rpa_job(session, "collect_amazon_review", {})
            first = await claim_next_rpa_job(session)
            assert first.job_id == a["job_id"]  # 最老优先
            assert first.status == STATUS_RUNNING
            assert first.started_at is not None
            second = await claim_next_rpa_job(session)
            assert second.job_id == b["job_id"]
            assert await claim_next_rpa_job(session) is None

    @pytest.mark.asyncio
    async def test_finish_done(self, use_session_factory):
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "query_campaign_spend", {"start_date": "2026-06-01"})
            await claim_next_rpa_job(session)
            await finish_rpa_job(session, job["job_id"], status=STATUS_DONE, result={"rows": 3})
            got = await get_rpa_job(session, job["job_id"])
            assert got["status"] == STATUS_DONE
            assert got["finished_at"]
            assert json.loads(got["result"]) == {"rows": 3}
            assert got["error"] is None

    @pytest.mark.asyncio
    async def test_finish_failed(self, use_session_factory):
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})
            await claim_next_rpa_job(session)
            await finish_rpa_job(session, job["job_id"], status=STATUS_FAILED, error="登录失败")
            got = await get_rpa_job(session, job["job_id"])
            assert got["status"] == STATUS_FAILED
            assert got["error"] == "登录失败"
            assert got["result"] is None

    @pytest.mark.asyncio
    async def test_list_filters_status(self, use_session_factory):
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            await create_rpa_job(session, "query_campaign_spend", {"start_date": "2026-06-01"})
            await create_rpa_job(session, "collect_amazon_review", {})
            await create_rpa_job(session, "update_track_table", {"store": "天安"})
            queued = await list_rpa_jobs(session, status=STATUS_QUEUED)
            assert len(queued) == 3
            all_jobs = await list_rpa_jobs(session, limit=2)
            assert len(all_jobs) == 2  # limit 生效

    @pytest.mark.asyncio
    async def test_requeue_stale_running(self, use_session_factory):
        """启动时把残留 running 重置回 queued（at-least-once）。"""
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "update_track_table", {"store": "巧逗豆"})
            await claim_next_rpa_job(session)  # → running
        await _requeue_stale_running()
        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
            assert got["status"] == STATUS_QUEUED


# ═══════════════════════════════════════════════════
# submit 工具
# ═══════════════════════════════════════════════════


class TestSubmitTools:
    @pytest.mark.asyncio
    async def test_submit_query_campaign_spend(self, use_session_factory):
        res = await submit_rpa_query_campaign_spend.ainvoke(
            {"start_date": "2026-06-01", "end_date": "2026-06-30", "output_dir": ""}
        )
        assert res["status"] == "submitted"
        assert res["job_type"] == "query_campaign_spend"
        assert res["job_id"].startswith("rpa-")
        assert res["job_id"] in res["message"]

    @pytest.mark.asyncio
    async def test_submit_collect_amazon_review(self, use_session_factory):
        res = await submit_rpa_collect_amazon_review.ainvoke({"excel_path": ""})
        assert res["status"] == "submitted"
        assert res["job_type"] == "collect_amazon_review"

    @pytest.mark.asyncio
    async def test_submit_update_track_table(self, use_session_factory):
        res = await submit_rpa_update_track_table.ainvoke({"store": "巧逗豆"})
        assert res["status"] == "submitted"
        assert res["job_type"] == "update_track_table"

    @pytest.mark.asyncio
    async def test_submit_error_path(self, use_session_factory, monkeypatch):
        """DB 不可用时返回 error 摘要，不抛异常（对话回合不炸）。"""

        class BrokenCtx:
            async def __aenter__(self):
                raise RuntimeError("db down")

            async def __aexit__(self, *a):
                return False

        class BrokenFactory:
            def __call__(self):
                return BrokenCtx()

        monkeypatch.setattr(rpa_jobs, "_session_factory", BrokenFactory())
        res = await submit_rpa_collect_amazon_review.ainvoke({"excel_path": "/x.xlsx"})
        assert res["status"] == "error"
        assert "db down" in res["message"]

    @pytest.mark.asyncio
    async def test_submit_does_not_execute(self, use_session_factory):
        """提交只入队（queued），不跑真实执行。"""
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            await submit_rpa_update_track_table.ainvoke({"store": "天安"})
            all_jobs = await list_rpa_jobs(session)
        assert len(all_jobs) == 1
        assert all_jobs[0]["status"] == STATUS_QUEUED


# ═══════════════════════════════════════════════════
# 调度器（DRY_RUN 分支）
# ═══════════════════════════════════════════════════


class TestDispatcherDryRun:
    @pytest.mark.asyncio
    async def test_run_on_executor_dry_run(self, monkeypatch):
        monkeypatch.setenv("RPA_DRY_RUN", "1")
        text = await get_rpa_dispatcher()._run_on_executor(
            "collect_amazon_review", {"excel_path": ""}
        )
        data = json.loads(text)
        assert data["dry_run"] is True
        assert data["job_type"] == "collect_amazon_review"

    @pytest.mark.asyncio
    async def test_dispatcher_start_stop(self, use_session_factory):
        """start()/stop() 生命周期：loop 可正常取消、不悬挂、幂等。"""
        d = get_rpa_dispatcher()
        try:
            await d.start()
            assert d.running
            await d.start()  # 幂等：重复 start 为 no-op
            assert d.running
        finally:
            await d.stop()
        assert not d.running
        await d.stop()  # 幂等

    @pytest.mark.asyncio
    async def test_dispatch_next_dry_run_full_cycle(self, use_session_factory, monkeypatch):
        """全链路：queued →（调度器认领）→ done，结果落库。"""
        monkeypatch.setenv("RPA_DRY_RUN", "1")
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "update_track_table", {"store": "巧逗豆"})

        await get_rpa_dispatcher()._dispatch_next()

        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
        assert got["status"] == STATUS_DONE
        assert got["finished_at"]
        assert json.loads(got["result"])["dry_run"] is True

    @pytest.mark.asyncio
    async def test_dispatch_noop_when_empty(self, use_session_factory, monkeypatch):
        monkeypatch.setenv("RPA_DRY_RUN", "1")
        await get_rpa_dispatcher()._dispatch_next()  # 空队列不抛

    @pytest.mark.asyncio
    async def test_dispatch_marks_failed_on_error(self, use_session_factory, monkeypatch):
        """执行抛错 → 任务标 failed + 记录错误。"""
        monkeypatch.setenv("RPA_DRY_RUN", "1")
        factory = rpa_jobs._get_session_factory()
        async with factory() as session:
            job = await create_rpa_job(session, "collect_amazon_review", {})

        async def _boom(*a, **k):
            raise RuntimeError("executor 挂了")

        monkeypatch.setattr(rpa_jobs.RpaJobDispatcher, "_run_on_executor", _boom)
        await get_rpa_dispatcher()._dispatch_next()

        async with factory() as session:
            got = await get_rpa_job(session, job["job_id"])
        assert got["status"] == STATUS_FAILED
        assert "executor 挂了" in got["error"]


# ═══════════════════════════════════════════════════
# MCP 结果解析
# ═══════════════════════════════════════════════════


class TestMcpResultText:
    def test_ok_content_joined(self):
        result = SimpleNamespace(isError=False, content=[SimpleNamespace(text='{"ok": 1}')])
        assert _mcp_result_text(result, "rpa_x") == '{"ok": 1}'

    def test_error_flag(self):
        result = SimpleNamespace(isError=True, content=[SimpleNamespace(text="boom")])
        data = json.loads(_mcp_result_text(result, "rpa_x"))
        assert data["status"] == "error"
        assert data["message"] == "boom"

    def test_none_result(self):
        data = json.loads(_mcp_result_text(None, "rpa_x"))
        assert data["status"] == "success"

    def test_multi_content_blocks(self):
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text="a"), SimpleNamespace(text="b")],
        )
        assert _mcp_result_text(result, "rpa_x") == "a\nb"
