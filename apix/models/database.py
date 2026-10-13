"""
Async database session management for APIx.

Provides:
- AsyncEngine and async_sessionmaker factory tied to DATABASE_URL
- get_db() async generator for FastAPI dependency injection
- init_db() / close_db() lifecycle helpers

Usage in FastAPI:
    from apix.models.database import get_db
    @app.get("/items")
    async def list_items(db: AsyncSession = Depends(get_db)):
        ...
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apix.config.settings import get_settings

# ── Module-level singletons (initialized lazily by init_db) ──────
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it if necessary."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.log_level.upper() == "DEBUG",
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the singleton session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an async database session.

    The session is automatically closed after the request completes.
    Callers should commit explicitly; this generator only ensures cleanup.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """
    Create all tables defined in db_models.Base.metadata.

    Called during FastAPI lifespan startup. In production, prefer
    Alembic migrations; this is for development convenience.
    """
    from apix.models.db_models import Base

    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Dispose the async engine connection pool. Called during shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
