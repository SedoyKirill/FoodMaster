"""Nightly database backup job."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import structlog

from app.core import backup
from app.core.clock import now
from app.core.config import get_settings

log = structlog.get_logger(__name__)

#: If the newest dump is older than this at scheduler start, run one right away.
CATCH_UP_AFTER = timedelta(hours=24)


async def backup_job() -> None:
    """Run pg_dump off the event loop (it is a blocking subprocess)."""
    settings = get_settings()
    log.info("backup.started", target=str(settings.backup_local_dir))
    await asyncio.to_thread(backup.run_backup, settings)


async def catch_up_if_stale() -> None:
    """Make "daily backups" true on a PC that is switched off at night.

    A plain cron entry at 04:30 simply never fires if the family shuts the
    machine down at 23:00 — and you find out three months later, when you need
    the backup. APScheduler's misfire grace covers a late start; this covers a
    machine that was off for the whole window.
    """
    settings = get_settings()
    newest = backup.latest_dump(settings.backup_local_dir)
    if newest is not None:
        stamp = newest.stem.removeprefix(backup.FILENAME_PREFIX)
        try:
            from datetime import datetime

            taken_at = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=now().tzinfo)
        except ValueError:
            taken_at = None
        if taken_at is not None and now() - taken_at < CATCH_UP_AFTER:
            log.info("backup.catch_up_skipped", newest=newest.name)
            return

    log.info("backup.catch_up", reason="no recent dump found")
    await backup_job()
