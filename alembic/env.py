"""Alembic environment: async engine, pgvector awareness, single-writer guard."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

import pgvector.sqlalchemy
from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.core.models import Base  # imports every model module -> complete metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata

# Arbitrary but stable. Guards against api and scheduler migrating at once.
MIGRATION_LOCK_ID = 0x5241_5449


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Autogenerate renders pgvector types but does not import them."""
    if type_ == "type" and isinstance(obj, pgvector.sqlalchemy.Vector):
        autogen_context.imports.add("import pgvector.sqlalchemy")
        return f"pgvector.sqlalchemy.Vector({obj.dim})"
    return False


def do_run_migrations(connection: Connection) -> None:
    # Reflection has no hook for third-party types; injecting into the
    # dialect's registry is how `alembic check` learns to read vector columns.
    connection.dialect.ischema_names["vector"] = pgvector.sqlalchemy.Vector

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
        include_schemas=False,
    )

    with context.begin_transaction():
        # Taken as the transaction's first statement, before alembic_version is
        # read: a second process blocks here, and when it unblocks it sees the
        # winner's committed revision and finds nothing to do. Released
        # automatically on commit or rollback, so a killed container cannot
        # leak it.
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_ID})
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
