"""
app/database.py
───────────────
Async SQLAlchemy engine and session management.
All database interaction goes through `get_db()` dependency.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


# ── Engine ────────────────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.async_database_url,
    echo=settings.app_debug,         # SQL logging in development
    pool_pre_ping=True,              # detect stale connections
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600,               # recycle connections after 1 h
)

# ── Session factory ───────────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ── Declarative base ──────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── FastAPI dependency ────────────────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields an AsyncSession per request and ensures it is closed after the
    response is sent (even on exceptions).

    Usage:
        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
