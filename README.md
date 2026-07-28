# Ration — smart meal & grocery planner

Self-hosted web app: a recipe database with semantic search, nightly price
scraping of nearby grocery stores (Pyaterochka, Lenta), a 3–4 day meal plan
tailored to each family member's calorie needs, and an optimized shopping list —
all in a **single** store, the one where the whole basket is cheapest, sized so
food doesn't get thrown away. Tracks money spent and actual nutrition facts.

Learning goals: hands-on RAG (embeddings + pgvector + retrieval), local LLM
inference, a modern typed frontend (React + OpenAPI).

> Русская версия: [README.ru.md](README.ru.md). Working module specs
> (`TZ-M*.md`) are maintained in Russian only.

## Target environment

- **Host**: desktop PC, Windows 11, RTX 3070 8 GB VRAM, Docker Desktop (WSL2).
- **Users**: one family. UI — browser at `http://localhost:8000`.
  Optional access from phones over home Wi-Fi (`WEB_EXPOSE=1`).
- **Everything in Docker**: Postgres, Ollama (GPU passthrough), API+web, task
  scheduler — a single `docker compose up -d` starts the whole system,
  `docker compose down` stops it.
- No auth: the app lives on the local network only and is never exposed
  to the internet.

## Stack

| Layer | Technology |
|---|---|
| Backend language | Python 3.12 |
| API server | FastAPI + uvicorn |
| Database | PostgreSQL 16 + pgvector (Docker) |
| Migrations | Alembic |
| ORM | SQLAlchemy 2.x (async) |
| Frontend | React 18 + Vite + TypeScript (strict) |
| UI libraries | Mantine (components), TanStack Query (data), Recharts (charts) |
| API contract | OpenAPI (FastAPI) → openapi-typescript → frontend types |
| LLM | Ollama locally, configurable model (`OLLAMA_MODEL`, default qwen3:8b; benchmark — TZ-M5); abstraction allows Claude API / no LLM |
| Embeddings | sentence-transformers `intfloat/multilingual-e5-small` on CPU |
| Purchase optimization | Google OR-Tools (CP-SAT) with a greedy fallback |
| Task scheduler | APScheduler (nightly price scraping) |

## Architecture

```
┌─────────────────────────────── PC (Windows + Docker) ───────────────────────────────┐
│                                                                                      │
│  Browser ── http://localhost:8000 ──► [M6 React SPA (static)]                        │
│                     │  /api/v1/*                                                     │
│                     ▼                                                                │
│               [FastAPI core M1]                                                      │
│                                                                                      │
│   [M2 Recipe RAG]  [M4 Nutrition math]  [M5 Planner]  [M7 Finance/diary]             │
│          │                 │                 │              │                        │
│          └─────────────────┴───────┬─────────┴──────────────┘                        │
│                                    ▼                                                 │
│                    [PostgreSQL + pgvector (Docker)]                                  │
│                                    ▲                                                 │
│  5ka / lenta ◄── nightly scrape ── [M3 store scrapers + APScheduler]                 │
│                                                                                      │
│  [Ollama :11434  Qwen3-8B]  ◄── LLMProvider (abstraction, removable layer)           │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

Two key principles:

1. **All math is deterministic code** (calories, macros, cooking weight loss,
   purchase optimization). The LLM is a thin removable layer: user's free text →
   parameters; planner result → human explanation. The app must fully work in
   button-only mode without an LLM (`NoLLMProvider`).
2. **API-first**: the web UI is just one client of the REST API. A Telegram bot
   or a mobile app can be added later as another thin client without rewriting
   the core.

## Modules and build order

| # | Spec | Module | Depends on |
|---|---|---|---|
| M1 | [TZ-M1.md](TZ-M1.md) | Core: DB schema, FastAPI skeleton, config | — |
| M2 | [TZ-M2.md](TZ-M2.md) | Recipe base + RAG (scraping, embeddings, search) | M1 |
| M3 | [TZ-M3.md](TZ-M3.md) | Store scrapers (Pyaterochka, Lenta) + product↔ingredient matching | M1, M2 |
| M4 | [TZ-M4.md](TZ-M4.md) | Family profiles & nutrition math (calories, cooking loss) | M1 |
| M5 | [TZ-M5.md](TZ-M5.md) | Meal planner + purchase optimizer + LLM layer | M1–M4 |
| M6 | [TZ-M6.md](TZ-M6.md) | Web UI (React SPA) + API conventions | M1 (skeleton), M2–M5 (pages) |
| M7 | [TZ-M7.md](TZ-M7.md) | Food diary, finances, analytics | M1, M3–M6 |

Recommended order: M1 → M2 → M4 → M3 → M5 → M6 → M7. The web UI skeleton
(navigation, Family page) can be brought up right after M1 and grown page by
page as modules land. The LLM part is wired in last — everything must work on
buttons and algorithms first.

## Repository layout

```
ration/
├── docker-compose.yml        # db, ollama(GPU), api, scheduler
├── Dockerfile                # multi-stage: web build → Python image
├── .env.example
├── pyproject.toml
├── alembic/                  # migrations
├── app/
│   ├── core/                 # M1: config, DB, shared models
│   ├── recipes/              # M2: recipes, embeddings, search
│   ├── store/                # M3: store adapters (5ka, lenta), matching
│   ├── nutrition/            # M4: calories, macros, cooking loss
│   ├── planner/              # M5: meal plan, optimizer, llm/
│   ├── finance/              # M7: diary, expenses, analytics
│   └── api/                  # FastAPI routes /api/v1 (per module)
├── web/                      # M6: React SPA (Vite + TypeScript)
├── docs/                     # public docs: *.md (EN) + *.ru.md (RU)
└── scripts/                  # one-off scripts: recipe import, embedding backfill
```

## Languages & documentation

- Code, docstrings, commit messages — **English**.
- Public docs (`README`, `docs/`) — bilingual: `<name>.md` (EN) + `<name>.ru.md` (RU).
- Working specs (`TZ-M*.md`) — Russian only.
- API docs are auto-generated: Swagger UI at `/docs` (from the FastAPI
  OpenAPI schema).

## Shared conventions (all modules)

- All product quantities in the DB are **grams/milliliters of the raw product**.
  Nutrition facts are stored per 100 g raw; cooked weight is derived via M4
  coefficients.
- Money is stored in kopecks (int) to avoid float errors.
- All external calls (5ka API, Ollama) have timeouts and retries; their failure
  must not bring the system down.
- Every module ships with tests for its math (pytest); especially M4 (formulas)
  and M5 (optimizer). Frontend — strict TypeScript.
- Every module updates `docs/` (EN + RU).
- Secrets and local settings live only in `.env`, never in git.

## Data & backups

- All data lives in Docker volumes on the PC's local disk. Never put the
  Postgres data directory on a NAS network share: network filesystems don't
  provide reliable file locking — a recipe for database corruption; besides,
  a home NAS's ARM CPU and 1 GB RAM couldn't handle Postgres+pgvector.
- Optional NAS setup: a nightly scheduler job runs `pg_dump`; if `BACKUP_DIR`
  (a mounted NAS SMB share) is set in `.env`, the dump is copied there
  (rotation — TZ-M1). If unset, dumps stay in a local volume.
- Restore: `scripts/restore.ps1 <dump>` (TZ-M1).

## Quick start (one-time, part of M1)

1. Install Docker Desktop (WSL2 backend), enable GPU support
   (Settings → Resources → WSL integration; recent NVIDIA driver).
2. `copy .env.example .env` (defaults are fine for local use).
3. `docker compose up -d` — brings up everything: Postgres, Ollama (pulls
   qwen3:8b ~5.2 GB on first start), API with migrations, task scheduler.
4. Open `http://localhost:8000` — the first-run wizard creates your family.

Daily use: `docker compose up -d` to start, `docker compose down` to stop.
DB data lives in volumes and survives shutdowns. No Python or Node needed on
the host — everything runs in containers (local installs are only for
development/tests).

## Future extensions (beyond v1)

- Telegram bot / Mini App as a second client of the same API.
- Receipt import: QR → Russian tax service (FNS) receipt API (line-item
  purchase data); receipt photo → vision model.
- More stores (Magnit, Perekrestok) — new `StoreAdapter` implementations (M3).
- CSV data export.
