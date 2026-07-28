# Configuration

The single source of settings is `.env` in the repository root. It is
git-ignored; `.env.example` is the committed template and must list every
variable the code reads.

> Русская версия: [configuration.ru.md](configuration.ru.md)

`.env` has two readers: **Docker Compose** (for interpolation in
`docker-compose.yml`) and **the application** (through `app/core/config.py`,
pydantic-settings). It therefore contains keys the application ignores
(`POSTGRES_PASSWORD`, `WEB_BIND_IP`, `DB_PORT`); `extra="ignore"` in the model
config handles that.

Compose overrides exactly two values for the containers — `DATABASE_URL` and
`OLLAMA_URL` — because those are the only ones that differ between "inside
Docker" and "on the host". `.env` holds the Docker-side truth, and
`scripts/dev.ps1` injects the host-side one as process environment, which
pydantic-settings ranks above the `.env` file. There is deliberately no separate
`.env.dev`: it would drift within a week.

## Application

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `APP_ENV` | `prod\|dev\|test` | `prod` | Run mode |
| `APP_TIMEZONE` | string | `Europe/Moscow` | The family's timezone. Drives the nightly job times and "what day is it" (see `app/core/clock.py`). Containers run in UTC, so without it the 03:00 parser would record prices under the previous date |
| `LOG_FORMAT` | `json\|console` | `json` | `console` is nicer for local development |
| `LOG_LEVEL` | string | `INFO` | |

## Database

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `ration` | Credentials of the `db` service. Compose-only |
| `DB_PORT` | `5432` | Host port, bound to `127.0.0.1` only. Needed by `dev.ps1`, pytest and any DB client |
| `DATABASE_URL` | `postgresql+asyncpg://ration:ration@db:5432/ration` | **Must start with `postgresql+asyncpg://`** — enforced by a validator, otherwise the failure surfaces deep inside SQLAlchemy |

## Web

| Variable | Default | Purpose |
|---|---|---|
| `WEB_BIND_IP` | `127.0.0.1` | `0.0.0.0` opens access from the home network. Replaces the spec's `WEB_EXPOSE` — see [setup.md](setup.md) |
| `WEB_PORT` | `8000` | Host port |

## LLM

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `none` | `none` is the fully button-driven mode; everything works without an LLM. Also `ollama`, `claude` |
| `OLLAMA_URL` | `http://ollama:11434` | |
| `OLLAMA_MODEL` | `qwen3:8b` | Changing it and running `docker compose up -d` pulls the new model automatically |
| `OLLAMA_KEEP_ALIVE` | `10m` | How long the model stays resident in VRAM. A cold load costs 5-10 s of M5's 15 s budget |
| `OLLAMA_TIMEOUT_S` | `120` | |
| `ANTHROPIC_API_KEY` | empty | Required when `LLM_PROVIDER=claude`; validated at startup rather than during the first plan generation at 22:00 |

## Embeddings (M2)

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | `intfloat/multilingual-e5-small` | |
| `EMBEDDING_DIM` | `384` | Checked at startup against the schema's `VECTOR(384)`; a mismatch is a startup error, because changing the dimension requires a migration |

## Stores (M3)

| Variable | Default | Purpose |
|---|---|---|
| `STORES` | `5ka,lenta` | Comma-separated. The field is marked `NoDecode`; otherwise pydantic-settings would try to JSON-decode it and fail at startup |
| `FIVEKA_STORE_ID`, `LENTA_STORE_ID` | empty | Ids of the nearest shops; filled in during M3 |
| `STORE_REQUEST_DELAY_S` | `1.0` | At most one request per second |

## Scheduler and backups

| Variable | Default | Purpose |
|---|---|---|
| `PARSE_CRON_HOUR` / `PARSE_CRON_MINUTE` | `3` / `0` | Nightly catalogue collection (M3) |
| `BACKUP_CRON_HOUR` / `BACKUP_CRON_MINUTE` | `4` / `30` | Deliberately after the parse, so the dump contains the night's prices |
| `BACKUP_DIR` | empty | Secondary dump destination, a path **inside the container**. See [backup-restore.md](backup-restore.md) |
| `BACKUP_KEEP` | `14` | Dumps kept in each location |
| `NAS_HOST`, `NAS_SHARE`, `NAS_USER`, `NAS_PASSWORD` | empty | Only used by `docker-compose.nas.yml` |

## Image build

| Variable | Default | Purpose |
|---|---|---|
| `APP_EXTRAS` | empty | Becomes `--extra ml` in M2, adding `sentence-transformers` and the CPU build of torch to the image |
