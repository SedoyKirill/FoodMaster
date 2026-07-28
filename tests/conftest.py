"""Test fixtures.

The test database is a separate database inside the compose `db` service rather
than a throwaway container. On Docker Desktop / WSL2 container startup dominates
everything: reusing the already-warm Postgres makes the suite start in under a
second instead of ten, which is the difference between running the tests and
not bothering.

The schema is created once per session with `alembic upgrade head` — not with
`metadata.create_all()`, which would skip CREATE EXTENSION and the seed rows and
would never exercise the migration production actually runs.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "ration_test"
DEFAULT_TEST_URL = f"postgresql+asyncpg://ration:ration@127.0.0.1:5432/{TEST_DB_NAME}"


def test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)


def _admin_url() -> str:
    return test_database_url().rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="session", autouse=True)
def _configure_environment() -> Iterator[None]:
    """Point the whole application at the test database before anything imports it."""
    url = test_database_url()
    # Guard: the fixture below drops this database.
    assert url.rsplit("/", 1)[-1].endswith("_test"), (
        "refusing to run tests against a database whose name does not end in _test"
    )
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    os.environ["APP_ENV"] = "test"
    os.environ["LOG_FORMAT"] = "console"

    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous
    get_settings.cache_clear()


def _postgres_reachable() -> bool:
    async def probe() -> bool:
        engine = create_async_engine(_admin_url(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(probe())


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    return _postgres_reachable()


@pytest.fixture(scope="session")
async def migrated_database(postgres_available: bool) -> AsyncIterator[None]:
    if not postgres_available:
        pytest.skip("PostgreSQL is not reachable; start it with `docker compose up -d db`")

    admin = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": TEST_DB_NAME},
        )
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    await admin.dispose()

    from app.core.migrate import upgrade_to_head

    # alembic's env.py runs asyncio.run(); it needs a thread of its own.
    await asyncio.to_thread(upgrade_to_head, test_database_url())
    yield


@pytest.fixture(scope="session")
async def engine(migrated_database: None) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(test_database_url(), poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One outer transaction per test, always rolled back.

    `join_transaction_mode="create_savepoint"` lets the code under test call
    commit() freely: it commits a savepoint, and the outer transaction still
    survives to be discarded.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async_session = AsyncSession(
            bind=connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield async_session
        finally:
            await async_session.close()
            await transaction.rollback()


@pytest.fixture(scope="session")
def app() -> FastAPI:
    from app.main import create_app

    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the ASGI app.

    httpx.ASGITransport does not run the lifespan, hence LifespanManager.
    raise_app_exceptions=False is what lets the error-envelope tests inspect a
    500 response instead of having the exception re-raised into the test.
    """
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
