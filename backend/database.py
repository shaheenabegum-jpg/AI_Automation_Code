"""
Async PostgreSQL connection via SQLAlchemy 2.0 + asyncpg.
Sync engine for Alembic migrations only.
"""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import create_engine
from config import settings

# ── Async engine (used by FastAPI) ──────────────────────────────────────────────
async_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ── Sync engine (Alembic only) ──────────────────────────────────────────────────
sync_engine = create_engine(settings.SYNC_DATABASE_URL, echo=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency: yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    """Create all tables on startup (dev only). Use Alembic in production."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
