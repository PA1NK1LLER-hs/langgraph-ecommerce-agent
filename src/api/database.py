"""数据库引擎和会话管理。"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import POSTGRES_URL

# PostgreSQL 异步引擎（复用 docker-compose 中的 PostgreSQL 容器）
DATABASE_URL = POSTGRES_URL.replace("postgresql://", "postgresql+asyncpg://") if POSTGRES_URL else ""

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5, max_overflow=10) if DATABASE_URL else None
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False) if engine else None


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI 依赖：获取数据库会话。"""
    if async_session is None:
        raise RuntimeError("POSTGRES_URL not configured — database unavailable")
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """创建所有表（在应用启动时调用）。"""
    if engine is None:
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
