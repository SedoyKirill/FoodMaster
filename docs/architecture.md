# Architecture

> Русская версия: [architecture.ru.md](architecture.ru.md)

## Two principles

1. **All arithmetic is deterministic code.** Calories, macros, cooking loss,
   portion sizes, purchase optimization. The LLM is a thin removable layer that
   does exactly two things: turn free text into `PlanParams`, and turn a
   finished plan into human prose. The application must work completely with
   `LLM_PROVIDER=none`. **Allergies and exclusions are SQL filters** — never a
   similarity threshold, never a model's decision.
2. **API-first.** The web interface is one client of the REST API. A Telegram
   bot or a mobile app is added as another thin client, without rewriting the
   core.

## Processes

```
┌── PC (Windows + Docker Desktop, WSL2) ─────────────────────────────┐
│                                                                     │
│  Browser ── 127.0.0.1:8000 ──► [api]  FastAPI + uvicorn             │
│                                  │                                  │
│                                  ├── /api/v1/*   REST               │
│                                  ├── /docs       Swagger UI         │
│                                  ├── /health     probe              │
│                                  └── /*          SPA (from M6)      │
│                                  │                                  │
│  [scheduler]  APScheduler ───────┤                                  │
│    03:00 catalogue scrape (M3)   │                                  │
│    04:30 database backup (M1)    ▼                                  │
│                            [db]  PostgreSQL 16 + pgvector           │
│                                                                     │
│  [ollama]  qwen3:8b on the GPU ◄── LLMProvider (removable, M5)      │
│  [ollama-init]  one-shot model pull, exits 0                        │
└─────────────────────────────────────────────────────────────────────┘
```

`api` and `scheduler` are **the same image with different commands**. The only
behavioural difference comes from `RUN_MIGRATIONS`: only `api` applies
migrations; `scheduler` waits until the schema exists.

## Code layers

```
app/
├── core/            the foundation everything depends on
│   ├── config.py      settings (pydantic-settings)
│   ├── clock.py       "what day is it" in the family's timezone
│   ├── db.py          engine, session factory, get_session dependency
│   ├── models/        the shared schema kernel: structure only, no behaviour
│   ├── errors.py      AppError hierarchy plus the response envelope
│   ├── logging.py     structlog
│   ├── middleware.py  request id and access log
│   ├── http.py        shared httpx client with timeouts
│   ├── backup.py      pg_dump, verify, copy, rotate
│   └── spa.py         the M1 <-> M6 static-serving seam
├── api/v1/          routers only, no business logic
├── recipes/         M2   search, embeddings, normalisation
├── store/           M3   store adapters, matching, prices
├── nutrition/       M4   norm and nutrition formulas
├── planner/         M5   dish selection, purchase optimizer, llm/
├── finance/         M7   diary, expenses, analytics
└── scheduler/       the APScheduler process and its jobs
```

**Layering rule:** `app/core/models` is the shared kernel. Feature packages
import from it, never the reverse. Not a matter of taste:
`alembic/env.py` does `from app.core.models import Base`, and models living in
feature packages would drag FastAPI routers, `sentence-transformers` and
`ortools` into the migration process.

## Request lifecycle

1. `RequestContextMiddleware` creates a `request_id` (or takes it from the
   `x-request-id` header) and binds it into structlog's context. From then on
   **every** log line in every module carries it, with no plumbing through
   function signatures.
2. A router in `app/api/v1/` handles the request; pydantic validates the input.
3. The `get_session` dependency yields a SQLAlchemy session. Handlers commit
   **explicitly** — an implicit commit would silently persist half-finished work
   from a handler that returned early.
4. Errors become the `{"error": {"code", "message", "details"}}` envelope via
   the handlers in `app/core/errors.py`.
5. The middleware appends `x-request-id` to the response and emits an access log
   line with the duration.

## Startup behaviour

`create_app()` **deliberately does not connect to the database.**
`create_async_engine` opens no connection; the first real connect happens on the
first request. As a result a database hiccup leaves the container up and
`/health` honestly reporting `degraded`, instead of a crash loop under
`restart: unless-stopped`.

Compose ordering: `db` → (healthy) → `api` (migrations, then uvicorn) →
(healthy) → `scheduler`. Ollama blocks nothing: `api` starts independently and
the model download runs in a separate one-shot container.

After a Windows reboot, Docker restarts containers **by restart policy and
ignores `depends_on`**. The entrypoint therefore waits for the database on its
own in every case, and `scheduler` additionally waits for the schema to appear;
`alembic/env.py` holds a transaction-scoped advisory lock in case migrations do
start concurrently anyway.

## What is already fixed for M6

- Every module router exists and is registered under `/api/v1`, including the
  empty ones: the URL space and the OpenAPI tags are stable from the first
  commit.
- `generate_unique_id_function` produces readable, stable `operationId`s. The
  default would be `search_recipes_api_v1_recipes_search_get`, which also
  changes whenever a URL changes — churning the generated frontend types for no
  reason.
- `separate_input_output_schemas=False`: otherwise every model with defaults
  splits into `FooInput`/`FooOutput`, doubling the generated TypeScript for no
  benefit.
- `ErrorResponse` is declared once at router level, so the schema carries one
  shared type instead of a per-endpoint copy.
- `mount_spa()` is already the last call and picks its own branch: while
  `web/dist` is absent it serves the placeholder; once it exists it serves the
  SPA. M6 will not have to touch `main.py`.
