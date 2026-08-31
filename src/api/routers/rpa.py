"""RPA 任务状态 API — 查询任务队列（提交走聊天审批流，不经本模块）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import get_current_user
from ..models import User as UserModel
from agent.rpa_jobs import get_rpa_job, list_rpa_jobs, JOB_STATUSES

router = APIRouter(prefix="/api/rpa", tags=["rpa"])


@router.get("/jobs")
async def list_jobs(
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
):
    """列出 RPA 任务（按提交时间倒序），可按状态过滤。"""
    if status and status not in JOB_STATUSES:
        raise HTTPException(status_code=422, detail=f"无效状态: {status}")
    jobs = await list_rpa_jobs(db, limit=limit, status=status)
    return {"jobs": jobs, "count": len(jobs)}


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: Annotated[UserModel, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """查询单个 RPA 任务详情（状态/结果/错误）。"""
    job = await get_rpa_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    return job
