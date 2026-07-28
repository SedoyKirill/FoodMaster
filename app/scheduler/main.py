"""The `scheduler` container: nightly jobs, nothing else.

Job definitions live in code with an in-memory job store on purpose. A
SQLAlchemy job store would create tables outside Alembic's control and let a
stale job definition survive a code change; the job set here is small and fixed.
"""

from __future__ import annotations

import asyncio
import signal
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.db import dispose_engine
from app.core.http import close_http_client
from app.core.logging import configure_logging
from app.scheduler.jobs.backup import backup_job, catch_up_if_stale

log = structlog.get_logger(__name__)

#: The family switches the PC off at night. Without a generous grace window a
#: job whose moment passed while the machine was off is silently skipped.
MISFIRE_GRACE_SECONDS = 6 * 3600


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.app_timezone))

    scheduler.add_job(
        backup_job,
        CronTrigger(hour=settings.backup_cron_hour, minute=settings.backup_cron_minute),
        id="db_backup",
        name="Nightly database backup",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )

    # M3 registers the store parse job here (PARSE_CRON_HOUR, deliberately
    # before the backup so the dump contains the night's prices).
    return scheduler


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")

    scheduler = build_scheduler()
    scheduler.start()
    log.info(
        "scheduler.started",
        timezone=settings.app_timezone,
        jobs=[job.id for job in scheduler.get_jobs()],
    )

    await catch_up_if_stale()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows dev runs
            signal.signal(sig, lambda *_: stop.set())

    try:
        await stop.wait()
    finally:
        log.info("scheduler.stopping")
        scheduler.shutdown(wait=False)
        await close_http_client()
        await dispose_engine()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
