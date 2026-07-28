# Development

> Русская версия: [development.ru.md](development.ru.md)

## Environment

The tool is [uv](https://docs.astral.sh/uv/). If it is missing, one command
installs it: `python -m pip install uv` (then write `python -m uv ...` instead of
`uv ...`).

```powershell
uv sync            # dev environment: dependencies plus the dev group
uv run pytest -q
uv run ruff check . ; uv run ruff format . ; uv run mypy
```

This is a *virtual project*: `[tool.uv] package = false`, so uv installs
dependencies only and never builds `app` itself. That keeps the Docker
dependency layer independent of the source tree — editing code does not
invalidate the build cache. The price is `PYTHONPATH=/app` in the image and
`pythonpath = ["."]` in the pytest config.

`uv.lock` is **committed**. It resolves for all platforms at once, so "works on
Windows" and "works in the container" cannot diverge. To bump one dependency:
`uv lock --upgrade-package fastapi`.

### Why torch comes from a separate index

`sentence-transformers` (M2) pulls torch. The regular PyPI torch wheel for Linux
is 526 MB and drags in ~2.5 GB of `nvidia-*` packages this project will never
use: embeddings run on the CPU, and the GPU belongs to Ollama in a separate
container. Hence, in `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = [{ index = "pytorch-cpu" }]
```

This is already resolved in `uv.lock`, so enabling M2 is an image rebuild, not a
re-resolution.

## Development mode

```powershell
.\scripts\dev.ps1
```

The script starts `db` (and `ollama`) in Docker and runs the application itself
on Windows with `--reload`. The reason: uvicorn's reloader relies on native
filesystem events, and those are not delivered reliably across the
Windows → WSL2 boundary — containerised hot reload degrades to CPU-hungry
polling or silently misses edits. A bonus: the production compose file needs no
source bind mount, which removes the whole Cyrillic-path risk class.

`.env` is **not modified**: it holds the Docker-side truth (`@db:5432`) and the
script injects the host-side values as process environment, which
pydantic-settings ranks above the file. A separate `.env.dev` would drift within
a week.

Useful switches: `-NoOllama`, `-ResetDb` (recreates the database — deliberately
not `docker compose down -v`, which would also destroy the 5 GB model volume),
`-Port`.

## Tests

```powershell
docker compose up -d db      # once
uv run pytest -q
```

The test database is a separate `ration_test` database **inside the same
container**, not a throwaway container. On Docker Desktop / WSL2 container
startup dominates everything: reusing the already-warm Postgres makes the suite
start in under a second instead of ten, which is the difference between running
the tests and not bothering.

The schema is created once per session with `alembic upgrade head`, not
`metadata.create_all()`: the latter would skip `CREATE EXTENSION` and the seed
rows and would never exercise the migration production actually runs. Each test
is wrapped in an outer transaction and rolled back;
`join_transaction_mode="create_savepoint"` lets the code under test call
`commit()` freely.

Tests marked `@pytest.mark.db` are skipped when Postgres is unreachable.

## Migrations

Every table already exists from migration `0001`. A new migration is only needed
when a module genuinely adds a table or a column.

```powershell
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
uv run alembic check      # do models and migrations agree?
```

`alembic check` is the highest-value check in the suite: it asserts that models
and migrations agree, the first invariant to rot once modules start adding
columns. It also runs in CI.

Autogenerate **cannot**: create extensions, detect CHECK-constraint changes, or
insert seed data. Those are hand-written, as in `0001`.

## Style

- Code, identifiers, docstrings, comments, commit messages and log event names
  are **English**. UI strings are Russian.
- Lint and type rules live in `pyproject.toml`. mypy runs in `strict` mode.
- `RUF001`-`RUF003` (ambiguous characters) are disabled: the UI is Russian and
  Cyrillic in string literals is normal.
- Log structurally: `log.info("plan.created", plan_id=7, store="lenta")`, not
  string formatting. `request_id` is added automatically.

## Branches and commits

One branch per module: `feat/m1-core`, `feat/m2-recipes`, … Merge into `main`
once the spec's acceptance criteria have actually been verified. Commit messages
are English, Conventional Commits, scoped by package name (`core`, `api`, `db`,
`recipes`, `store`, `planner`, `finance`, `web`, `docker`).

CI (`.github/workflows/ci.yml`) runs ruff, mypy, pytest against a pgvector
service container, an `upgrade → downgrade → upgrade` cycle and an image build on
every push and pull request.

## Known environment limits (relevant from M6)

- **Node 22.12 on the host is already too old.** `eslint@10` requires `^22.13`
  and `react-router@8` requires `>=22.22`. Upgrade to Node 24 LTS before M6.
- **The README's React 18 conflicts with current libraries.** `@mantine/core@9`
  peers `react ^19.2` and `react-router@8` peers `react >=19.2.7`. Staying on
  React 18 pins two libraries to their previous majors on day one. Decide in M6.
- **`openapi-typescript@7` declares a `typescript ^5` peer**, so the frontend has
  to pin TS 5.9 rather than 7.
