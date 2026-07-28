# Ration / FoodMaster — repository conventions

Self-hosted family meal and grocery planner. Python 3.12 / FastAPI / SQLAlchemy 2
async / PostgreSQL 16 + pgvector / Ollama / OR-Tools / APScheduler, with a React
SPA arriving in M6. Everything runs in Docker Compose on one Windows home PC.
**There is no authentication** — the app is LAN-only and must never be published
to the internet.

## Source of truth

`TZ-M1.md` … `TZ-M7.md` in the repository root are the module specifications
(Russian, authoritative). `README.md` / `README.ru.md` hold the cross-cutting
conventions. Where the code deviates from a spec, the deviation is documented in
`docs/` — see `docs/data-model.ru.md` for the schema ones. When code and spec
disagree and the deviation is not documented, ask; do not silently pick one.

## Hard rules

1. **Money is `int` kopecks.** Never float, never rubles — in the database, the
   API or Python. Column and variable names end in `_kop`.
2. **Quantities are grams/ml of the RAW product.** Nutrition is stored per 100 g
   raw. Cooked weight is derived through M4's `cook_coefficients` at read time.
   Cooking never changes calories (water leaves, energy does not); the single
   exception is +10 g of oil per fried portion when the recipe does not list oil.
3. **All arithmetic is deterministic Python.** Calories, macros, cooking loss,
   portioning, purchase optimization. The LLM is a removable layer that only
   (a) turns free text into `PlanParams` and (b) turns a finished plan into
   prose. Every feature must work end to end with `LLM_PROVIDER=none`.
   **Allergies and exclusions are SQL filters — never a similarity threshold,
   never an LLM decision.**
4. **Quantities are `Decimal`, not `float`.** Convert to `int` only at the two
   boundaries that require it: OR-Tools CP-SAT, and JSON serialization.
5. **Language split.** Code, identifiers, docstrings, comments, commit messages
   and log event names are **English**. UI strings are Russian and belong in one
   module (`web/src/i18n/strings.ts` from M6). `TZ-M*.md` are Russian.
6. **Docs are bilingual pairs.** Every `docs/<name>.md` has a
   `docs/<name>.ru.md` with the same structure. Updating one without the other
   is an incomplete change. `TZ-M*.md` are exempt.
7. **API changes require regenerated types** once M6 exists: after touching any
   route or schema, regenerate `web/src/api/schema.d.ts` and commit it. "No diff
   from a regeneration" is part of every module's definition of done.
8. **Math modules ship with tests.** No merge without pytest coverage of the
   formulas in `app/nutrition/` (M4) and `app/planner/` (M5). Those live in
   `tests/unit/` and need no database.
9. **Every external call has a timeout.** Store APIs, Ollama, HuggingFace. Their
   failure degrades one feature; it never returns a 500 and never kills a
   service. Use the shared client in `app/core/http.py`.
10. **Errors use the envelope.** Raise `AppError` subclasses from
    `app/core/errors.py`. The wire format is
    `{"error": {"code", "message", "details"}}`. Never return a bare
    `{"detail": ...}`.
11. **Secrets live only in `.env`,** which is git-ignored. `.env.example` is
    committed and must list every variable the code reads.

## Layout

- `app/core/` — config, db, clock, logging, errors, http, backup, the SPA seam,
  and `app/core/models/`, the shared schema kernel (schema only, zero behaviour)
- `app/api/v1/<area>.py` — routers only, no business logic
- `app/{recipes,store,nutrition,planner,finance}/` — one package per module;
  they import from `app.core.models`, never the reverse
- `app/scheduler/` — the APScheduler process (nightly scrape, nightly backup)
- `alembic/` migrations · `scripts/` PowerShell + container entrypoints ·
  `tests/` · `data/` reference CSVs (`data/raw/` is a git-ignored cache)

## Structural invariants

- `mount_spa(app, ...)` is **the last call** in `create_app()`. It registers a
  catch-all route; anything added after it is unreachable.
- New routers go in `app/api/v1/`, take a tag from `app/api/tags.py`, and are
  added to the loop in `app/api/router.py`. Never call `include_router` from
  `main.py`.
- `get_settings()` is `lru_cache`d and `get_engine()` is lazy — never
  module-level singletons; the tests depend on being able to reset them.
- Do not iterate `app.routes` to rewrite operationIds: since FastAPI 0.137 it is
  a tree, not a flat list. Use `generate_unique_id_function`.
- Every table already exists from migration `0001`. A later module adds a
  migration only when it genuinely needs a new table.
- **Do not add a `register_vector` event listener.** pgvector's SQLAlchemy type
  serialises vectors as text; registering asyncpg's binary codec makes the two
  layers fight. `tests/test_schema.py::test_vector_column_round_trips_through_asyncpg`
  guards this.
- Members and recipes are never hard-deleted: `family_members.is_active = false`
  and `recipes.status = 'archived'`. Foreign keys are designed around that.
- JSONB columns are always assigned wholesale from the Pydantic models in
  `app/core/models/json_shapes.py`; SQLAlchemy does not track in-place mutation.

## Commands

```powershell
uv sync                                   # dev environment
uv run pytest -q                          # tests (needs `docker compose up -d db`)
uv run ruff check . ; uv run ruff format . ; uv run mypy
uv run alembic upgrade head
.\scripts\dev.ps1                         # db+ollama in Docker, API on host with reload
docker compose up -d                      # the whole system
docker compose down                       # stop; data survives in named volumes
```

## Windows environment gotchas

- The compose project name is pinned with `name: ration`. The directory name is
  Cyrillic and normalises to `eda`, which would silently share volumes with any
  other folder that normalises the same way. Do not remove it.
- `.gitattributes` forces LF. A shell script that reaches a Linux image with
  CRLF fails with `no such file or directory`, which is almost ungoogleable.
- Postgres `PGDATA` lives in a named Docker volume. **Never** bind-mount it to a
  NAS or any network share — see `docker-compose.nas.yml` for the supported way
  to put *dumps* on a NAS.
- `.env` must be saved as UTF-8; it contains Russian comments.
