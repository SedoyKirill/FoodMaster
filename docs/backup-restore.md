# Backup and restore

> Русская версия: [backup-restore.ru.md](backup-restore.ru.md)

## How it works

The `scheduler` container runs `pg_dump` in custom format every night at 04:30
(in `APP_TIMEZONE`) and writes the result into the `backups` Docker volume. The
time is deliberately **after** the 03:00 price scrape, so the dump contains the
night's prices.

The sequence in `app/core/backup.py`:

1. The dump is written to `*.dump.part`.
2. The archive is verified with `pg_restore --list`, which reads only the table
   of contents. It is cheap and it catches truncation. A backup nobody has ever
   read back is a rumour, not a backup.
3. Only after a successful verification is the file renamed to `*.dump`.
   Rotation therefore only ever sees complete files, and an interrupted dump can
   never cause a good one to be deleted.
4. When `BACKUP_DIR` is set, a copy goes there using the same rename dance —
   which matters over SMB, where a copy can stall for minutes.
5. Rotation keeps the `BACKUP_KEEP` (default 14) newest dumps **in both
   locations**. Sorting is by filename: the `YYYYMMDD-HHMMSS` stem sorts
   lexicographically as it does chronologically, so the result does not depend
   on how truthful the NAS's mtimes are.

A backup failure never kills the scheduler: the error is logged and the process
carries on.

### Catch-up run

A plain cron entry on a home PC simply **never fires** if the machine is off at
night — and you find that out three months later, when you need the backup. Two
mitigations:

- `misfire_grace_time = 6 hours` with `coalesce=True`, so a late start still
  runs the job;
- at scheduler start the age of the newest dump is checked, and if it is older
  than 24 hours a backup runs immediately.

### Why `pg_dump` lives in the application image

`pg_dump` must not be newer than whatever you will eventually restore into:
PostgreSQL 17 bumped the custom-archive format version, and `pg_restore 16`
rejects such a file. Debian bookworm's main archive only ships
`postgresql-client-15`, which refuses to dump a 16 server. So the image installs
exactly `postgresql-client-16` from the PGDG repository.

Restoring, conversely, happens **inside the `db` container**, where the client
version matches the server by construction and the `backups` volume is already
mounted read-only.

## Copying to a NAS

Dumps always land in the `backups` volume. `BACKUP_DIR` names a **second**
destination as a path *inside the container*.

A NAS SMB share cannot be bind-mounted into a container: with the WSL2 backend
neither a Windows drive letter nor a UNC path is reachable from the container.
The Linux side has to mount the share, which is what the overlay does:

```powershell
# .env
BACKUP_DIR=/mnt/nas-backup
NAS_HOST=192.168.1.50
NAS_SHARE=backups
NAS_USER=ration
NAS_PASSWORD=...

docker compose -f docker-compose.yml -f docker-compose.nas.yml up -d
```

The `uid=10001,gid=10001` options in `docker-compose.nas.yml` must match the
image's `app` user, or the non-root process cannot write.

> **Only finished dump files go to the NAS.** The Postgres data directory must
> stay on a local disk: network filesystems do not provide honest file locking,
> and putting `PGDATA` on a share is a reliable way to corrupt the database.

## Restoring

```powershell
.\scripts\restore.ps1 -List                              # what is available
.\scripts\restore.ps1 -Dump ration-20260728-043000.dump  # from the backups volume
.\scripts\restore.ps1 -Dump 'D:\from-nas\ration-20260701-043000.dump'  # from disk
```

The script stops `api` and `scheduler`, recreates the database
(`DROP DATABASE ... WITH (FORCE)` — without `FORCE` the drop hangs behind any
stray connection), restores the dump, prints sanity values (installed
extensions, the Alembic revision, row counts) and starts the services again,
waiting for `/health` to answer.

**This is destructive.** Without `-Yes` the script requires the database name to
be typed manually.

### Do a restore drill once

TZ-M1's acceptance criteria include a verified restore. A restore path that has
never been exercised does not exist. The safe way to practise: create a separate
database, restore a dump into it, and compare row counts.
