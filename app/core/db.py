"""Async database engine, session factory and FastAPI dependency."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build a fresh engine. Tests use this directly; the app uses `get_engine`."""
    settings = settings or get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=30,
        pool_recycle=settings.db_pool_recycle,
        # This is a home PC that sleeps and hibernates, and WSL2 suspends the
        # VM. Without pre-ping the first request every morning fails with a
        # dead connection; the extra round trip costs ~0.2 ms on loopback.
        pool_pre_ping=True,
        connect_args={
            "timeout": 10,
            "command_timeout": 60,
            "server_settings": {
                # Distinguishes api from scheduler in pg_stat_activity when a
                # nightly parse blocks a UI query.
                "application_name": f"ration-{settings.app_role}",
                # Raw SQL that uses CURRENT_DATE must agree with app/core/clock.py.
                "timezone": settings.app_timezone,
                # JIT compilation is pure overhead on sub-100ms OLTP queries
                # and on pgvector distance calls.
                "jit": "off",
            },
        },
    )


def get_engine() -> AsyncEngine:
    """Lazily created process-wide engine."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,  # mandatory for async: avoids lazy IO after commit
            autoflush=False,
        )
    return _session_factory


async def dispose_engine() -> None:
    """Close all pooled connections (called from the app lifespan on shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session.

    Handlers commit explicitly. An implicit commit-on-success would silently
    persist half-finished work from a handler that returned early.
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Session for code outside FastAPI's DI: scheduler jobs and scripts."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db(timeout: float = 2.0) -> bool:
    """Cheap liveness probe for /health. Never raises."""
    try:
        async with asyncio.timeout(timeout):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
