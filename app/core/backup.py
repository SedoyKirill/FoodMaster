"""Database dumps: create, verify, copy, rotate.

Design notes that are not obvious from the code:

* The dump is written to ``*.dump.part`` and renamed only after it has been
  verified. Rotation therefore only ever sees complete files, so an interrupted
  04:30 dump can never cause a good one to be deleted.
* Verification is ``pg_restore --list``, which reads only the archive's table of
  contents. It is cheap and it catches truncation. A backup nobody has ever
  read back is a rumour, not a backup.
* Rotation sorts by filename. The ``YYYYMMDD-HHMMSS`` stem sorts
  lexicographically as it does chronologically, so this does not depend on the
  mtimes an SMB share reports.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import structlog

from app.core.clock import now
from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)

DUMP_SUFFIX = ".dump"
PARTIAL_SUFFIX = ".dump.part"
FILENAME_PREFIX = "ration-"


@dataclass(frozen=True)
class PgTarget:
    host: str
    port: int
    user: str
    password: str
    database: str


def parse_target(database_url: str) -> PgTarget:
    """Turn the SQLAlchemy URL into pg_dump connection parameters."""
    parsed = urlparse(database_url.replace("+asyncpg", "", 1))
    return PgTarget(
        host=parsed.hostname or "db",
        port=parsed.port or 5432,
        user=unquote(parsed.username or "postgres"),
        password=unquote(parsed.password or ""),
        database=(parsed.path or "/postgres").lstrip("/"),
    )


def dump_filename(moment: datetime | None = None) -> str:
    stamp = (moment or now()).strftime("%Y%m%d-%H%M%S")
    return f"{FILENAME_PREFIX}{stamp}{DUMP_SUFFIX}"


def _pg_env(target: PgTarget) -> dict[str, str]:
    # The password goes through the environment, never argv, so it cannot show
    # up in `ps` output.
    env = dict(os.environ)
    if target.password:
        env["PGPASSWORD"] = target.password
    return env


def create_dump(destination: Path, settings: Settings | None = None) -> Path:
    """Write a verified custom-format dump into `destination`. Returns its path."""
    settings = settings or get_settings()
    target = parse_target(settings.database_url)
    destination.mkdir(parents=True, exist_ok=True)

    final_path = destination / dump_filename()
    partial_path = final_path.with_suffix(".part")

    command = [
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-owner",
        "--no-privileges",
        f"--host={target.host}",
        f"--port={target.port}",
        f"--username={target.user}",
        f"--dbname={target.database}",
        f"--file={partial_path}",
    ]
    subprocess.run(
        command,
        check=True,
        env=_pg_env(target),
        timeout=settings.backup_timeout_s,
        capture_output=True,
    )

    verify_dump(partial_path)
    os.replace(partial_path, final_path)
    log.info("backup.created", path=str(final_path), bytes=final_path.stat().st_size)
    return final_path


def verify_dump(path: Path) -> None:
    """Raise if the archive cannot be read back."""
    subprocess.run(
        ["pg_restore", "--list", str(path)],
        check=True,
        capture_output=True,
        timeout=300,
    )


def copy_to_secondary(dump_path: Path, secondary_dir: Path) -> Path | None:
    """Copy a finished dump to the NAS share, atomically from a reader's view."""
    try:
        secondary_dir.mkdir(parents=True, exist_ok=True)
        final_path = secondary_dir / dump_path.name
        partial_path = final_path.with_suffix(".part")
        shutil.copyfile(dump_path, partial_path)
        os.replace(partial_path, final_path)
    except OSError as exc:
        # A NAS that is asleep must not fail the backup that already succeeded.
        log.error("backup.copy_failed", target=str(secondary_dir), error=str(exc))
        return None
    log.info("backup.copied", path=str(final_path))
    return final_path


def rotate(directory: Path, keep: int) -> list[Path]:
    """Delete all but the `keep` newest dumps. Returns what was removed."""
    if keep < 1:
        return []
    try:
        dumps = sorted(
            (p for p in directory.glob(f"{FILENAME_PREFIX}*{DUMP_SUFFIX}") if p.is_file()),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError as exc:
        log.error("backup.rotate_failed", directory=str(directory), error=str(exc))
        return []

    removed: list[Path] = []
    for stale in dumps[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError as exc:
            log.error("backup.unlink_failed", path=str(stale), error=str(exc))
    if removed:
        log.info("backup.rotated", directory=str(directory), removed=len(removed), kept=keep)
    return removed


def latest_dump(directory: Path) -> Path | None:
    try:
        dumps = sorted(directory.glob(f"{FILENAME_PREFIX}*{DUMP_SUFFIX}"), key=lambda p: p.name)
    except OSError:
        return None
    return dumps[-1] if dumps else None


def run_backup(settings: Settings | None = None) -> Path | None:
    """The whole nightly job. Never raises: a failure must not kill the scheduler."""
    settings = settings or get_settings()
    try:
        dump_path = create_dump(settings.backup_local_dir, settings)
    except subprocess.CalledProcessError as exc:
        log.error(
            "backup.failed",
            returncode=exc.returncode,
            stderr=(exc.stderr or b"").decode("utf-8", "replace")[-2000:],
        )
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("backup.failed", error=str(exc))
        return None

    if settings.backup_to_secondary:
        secondary = Path(settings.backup_dir)
        if copy_to_secondary(dump_path, secondary) is not None:
            rotate(secondary, settings.backup_keep)

    rotate(settings.backup_local_dir, settings.backup_keep)
    return dump_path
