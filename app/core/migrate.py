"""Run (or check) database migrations from Python.

Used by the container entrypoint so migrations do not need the alembic CLI on
PATH, and so both the "apply" and the "wait until applied" paths share one
implementation.

    python -m app.core.migrate            # alembic upgrade head
    python -m app.core.migrate --check    # exit 0 iff the schema is at head
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.core.config import PROJECT_ROOT, get_settings
from app.core.db import build_engine

ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def alembic_config(ini_path: Path | None = None, url: str | None = None) -> Config:
    cfg = Config(str(ini_path or ALEMBIC_INI))
    cfg.set_main_option("script_location", str((ini_path or ALEMBIC_INI).parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().database_url)
    return cfg


def upgrade_to_head(url: str | None = None) -> None:
    command.upgrade(alembic_config(url=url), "head")


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def _current_revision(url: str | None = None) -> str | None:
    settings = get_settings()
    engine = build_engine(
        settings if url is None else settings.model_copy(update={"database_url": url})
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
            return None if row is None else str(row[0])
    finally:
        await engine.dispose()


def is_at_head(url: str | None = None) -> bool:
    """True when the database schema matches the newest revision on disk."""
    try:
        current = asyncio.run(_current_revision(url))
    except Exception:
        return False
    return current is not None and current == head_revision()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply or verify database migrations.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not migrate; exit 0 only if the schema is already at head",
    )
    args = parser.parse_args(argv)

    if args.check:
        return 0 if is_at_head() else 1

    upgrade_to_head()
    return 0


if __name__ == "__main__":
    sys.exit(main())
