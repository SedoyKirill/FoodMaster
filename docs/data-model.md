# Data model

The schema lives in `app/core/models/` and is created in full by a single
migration, `alembic/versions/0001_initial_schema.py`, as TZ-M1 requires: every
table of the project is created there so that later modules never race each
other with migrations.

> Русская версия: [data-model.ru.md](data-model.ru.md)

## Layering

`app/core/models` is the **shared schema kernel: structure only, zero
behaviour.** Feature packages (`app/recipes/`, `app/store/`, `app/nutrition/`,
`app/planner/`, `app/finance/`) own queries and services and import from here,
never the reverse. This is not aesthetics: `alembic/env.py` does
`from app.core.models import Base`, and models living in feature packages would
drag FastAPI routers, `sentence-transformers` and `ortools` into the migration
process.

| File | Tables |
|---|---|
| `base.py` | `Base`, naming convention, type map, timestamp mixins |
| `enums.py` | closed vocabularies plus `pg_enum()` |
| `json_shapes.py` | Pydantic models for every JSONB column |
| `reference.py` | `ingredient_categories`, `cook_coefficients` |
| `family.py` | `families`, `family_members`, `weight_log` |
| `recipes.py` | `ingredients`, `recipes`, `recipe_ingredients` |
| `store.py` | `store_products`, `price_history`, `parse_runs` |
| `planning.py` | `meal_plans`, `plan_meals`, `plan_meal_portions`, `shopping_items`, `fridge` |
| `diary.py` | `meal_log`, `expenses` |

## Cross-cutting conventions

- **Money** is `INTEGER` kopecks; column names end in `_kop`.
- **Quantities** are `NUMERIC`, i.e. `Decimal` in Python — for the same reason
  as kopecks: the specs assert exact numbers (400 g of raw meat fried yields
  exactly 252 g; nutrition matches within ±2%). Convert to `int` only at the two
  boundaries that require it: CP-SAT in M5, and JSON serialization.
- **Instants** are `TIMESTAMPTZ`. **Calendar dates** are `DATE`, computed
  through `app/core/clock.py` rather than `date.today()`: containers run in UTC
  and the store parser runs at 03:00 Moscow time, when UTC is still on the
  previous day.
- **Closed vocabularies** are `VARCHAR(32)` plus a named CHECK, mirroring a
  Python `StrEnum`. Native PostgreSQL enums are avoided: Alembic cannot diff
  value additions, and `ALTER TYPE ... ADD VALUE` cannot run in the same
  transaction that uses the new value.
- **`store` columns** are plain `TEXT` with **no** CHECK. The set of stores is
  driven by the `STORES` variable and by which `StoreAdapter` implementations
  exist; a constraint there would mean a migration per new chain.
- **JSONB** is always assigned wholesale from a model in `json_shapes.py` —
  SQLAlchemy does not track in-place dict mutation.

## Vector search

Three `VECTOR(384)` columns — `recipes.embedding`, `ingredients.embedding`,
`store_products.embedding` — with HNSW indexes using `vector_cosine_ops`.

- The `vector` and `pg_trgm` extensions are created by the **migration**. The
  `pgvector/pgvector:pg16` image only installs the extension files; it does not
  run `CREATE EXTENSION`. Verified on a fresh volume.
- Parameters: `m=16, ef_construction=64` for recipes and ingredients (thousands
  of rows); `m=24, ef_construction=100` for store products (tens of thousands,
  and recall there directly determines M3's ">=90% auto-match accuracy"
  criterion).
- **No `register_vector` listener.** pgvector's SQLAlchemy type serialises
  vectors as text; registering asyncpg's binary codec makes the two layers
  fight. `test_vector_column_round_trips_through_asyncpg` guards this.
- Rows with `embedding IS NULL` are not in the HNSW index and never appear in
  results — correct (a recipe without an embedding is not searchable), but it
  means `backfill_embeddings` progress is visible as search coverage.
- **Relevant to M2:** with an approximate index, filters are applied *after* the
  index scan. A hard allergy filter can discard almost every candidate, turning
  20 results into 2. Fix it per query with
  `SET LOCAL hnsw.iterative_scan = strict_order` (pgvector >= 0.8; the image
  ships 0.8.5).

## Deviations from the TZ-M1 SQL sketch

The sketch in TZ-M1 section 2 is an outline, not DDL. Below is the full list of
changes with reasons. Each is reversible, but most are direct blockers for
M2-M7 — and the spec itself asks for the schema to be settled in M1.

### Missing pieces that made a module unimplementable

| Table | Added | Why |
|---|---|---|
| `meal_plans` | `start_date DATE NOT NULL` | `plan_meals.day` is a relative offset. Nothing tied a plan to the calendar, yet M6's dashboard shows "today's dishes" and M7's diary is date-keyed and compares plan against fact |
| `meal_plans` | `store TEXT` | M5's entire premise is one-store shopping; the optimizer compares baskets and picks a winner. There was nowhere to record the decision |
| `meal_plans` | `basket_comparison JSONB` | M6's third wizard step renders totals for every store, including the losers. Without persistence the page cannot be reopened without re-running CP-SAT |
| `meal_plans` | `explanation TEXT` | The output of `LLMProvider.explain_plan`. Otherwise every page load is a 15-second Ollama call, and the page breaks when Ollama is down |
| `meal_log` | `plan_meal_id FK` | M7's very first endpoint is `POST /diary/plan-meal {plan_meal_id, ...}`. `recipe_id` alone cannot say which slot was eaten (the same recipe may appear on day 1 and day 3) |
| `meal_log` | `items JSONB` | M7 parses free text into an ingredient list and requires entries to be editable. Storing only the four totals makes that impossible |
| `expenses` | `store_product_id`, `qty`, `store` | "A manual catalogue purchase becomes an expense **and** tops up the fridge" — the second half had no source of truth |
| `families` | `meal_split JSONB` | M4: the per-meal calorie split is "configurable per family". There was nowhere to put it |
| `ingredients` | `density_g_per_ml`, `piece_grams` | M2 converts "2 pieces" and "a glass" into grams; M3 converts "0.93 l". Without density, 0.93 l of oil becomes 930 g instead of ~856 g: a systematic error in price-per-gram ranking |
| `store_products` | `match_status`, `candidate_ingredient_id`, `match_source`, `matched_at` | M3 defines three matching outcomes including a **manual confirmation queue**, and M6 an admin page with yes/no buttons. The old shape could not distinguish auto-linked from candidate from human-rejected, so rejected pairs would be re-proposed every night and the queue would never drain |
| `recipes` | `status`, `quarantine_reason` | M2 quarantines recipes with >30% unresolved ingredient lines and M6 lists them in the admin area. There was no flag |
| `plan_meals` | `alternates JSONB` | M5 generates 1-2 spare candidates and M6 offers a "replace dish" button. The spares had nowhere to live |

### Structural fixes

| Table | Was | Now | Why |
|---|---|---|---|
| `recipe_ingredients` | PK `(recipe_id, ingredient_id)` | surrogate `id` + `UNIQUE (recipe_id, position)` | The composite key forbade the same ingredient twice in one recipe. "Butter into the dough" plus "butter to grease the tin" is everyday cooking; the importer would either crash or silently drop the second line |
| `recipe_ingredients` | `ingredient_id NOT NULL` | nullable, plus `raw_text`, `raw_amount`, `raw_unit` | M2 quarantines only recipes with >30% unresolved lines, so a recipe at 20% must still be storable and its unresolved lines need a home. Keeping the original text lets normalisation be re-run without re-scraping 5000 pages |
| `plan_meals` | `portions JSONB {member_id: multiplier}` | `plan_meal_portions` table | JSONB keys are always strings, there is no referential integrity, and M7's core report ("fact vs norm per member", "plan vs fact") would need `jsonb_each_text` plus casts in every query |
| `shopping_items` | `planned_price_kop` | `planned_unit_price_kop` + `actual_qty` | Ambiguous between unit price and line total. Getting it wrong is a silent 3x error in the budget check |
| `fridge` | balance or lot, unspecified | lots, plus `source`, `plan_id` | `expires_at` only makes sense per lot: two milk cartons bought a week apart. Consumers aggregate `SUM(grams) GROUP BY ingredient_id` |
| `store_products`, `parse_runs` | `store TEXT DEFAULT '5ka'` | no default | A default in a multi-store system invites silent misattribution |
| `parse_runs` | `running\|ok\|failed` | plus `partial`, `error`, `trigger` | A run that collected 8 of 10 categories is real and common. `stats.errors = 7` does not say what to fix; the message does |
| — | new `ingredient_categories` table | | `ingredients.category` and `cook_coefficients.ingredient_category` were two unrelated free-text fields joined by string equality. A typo would silently yield a factor of 1.0, and M4 would report raw weights as cooked ones |

### Indexes the spec did not list

The spec names the HNSW indexes and three b-trees. These were added, each
justified by a specific query in a module spec:

- `ix_recipe_ingredients_ingredient_id` — the **hot path of the allergy filter**
  in M2 and M5. The sketch's composite PK cannot serve a lookup by
  `ingredient_id` alone, so every plan generation would sequentially scan
  ~50 000 rows.
- `ix_store_products_ingredient_id_store` (partial) — `get_offers()` runs once
  per ingredient **per store**: ~120 times for a four-day plan.
- `ix_recipes_meal_type`, `ix_recipes_tags` (GIN) — M2's mandatory search filters.
- `ix_expenses_family_id_date` — `expenses` had no index at all in the spec, yet
  the whole M7 finance module is date-range aggregation.
- `ix_parse_runs_store_started_at` — M6's dashboard shows price freshness **per
  store**; an index on `started_at` alone cannot serve that.
- `uq_meal_plans_one_active` (partial unique) — the dashboard, the shopping list
  and the diary all say "the active plan" in the singular. This makes it an
  invariant.
- `uq_recipes_source_url` — M2's acceptance criterion is literally idempotent
  re-import by `source_url`.
- `uq_meal_log_plan_meal_member` (partial unique) — makes the "eaten" button
  idempotent, so double-tapping on a phone cannot log 800 kcal twice.

### Cached per-serving nutrition

TZ-M2 section 5 allows "a materialised field or on the fly" and picks on the
fly. The `kcal_per_serving` family of columns was added anyway, because
`max_kcal_per_serving` is a search **filter**: computing it on the fly means
aggregating `recipes ⋈ recipe_ingredients ⋈ ingredients` across the whole table
before the vector ordering can run. Whether to use them is M2's call; the
columns are nullable and break nothing.

## Deletion policy

- **CASCADE** where a child is meaningless without its parent: members and their
  logs from the family, recipe composition from the recipe, prices from the
  product, plan contents from the plan.
- **RESTRICT** where deletion would corrupt history: `plan_meals.recipe_id`,
  `shopping_items.product_id`, `fridge.ingredient_id`.
- **SET NULL** where the fact survives the reference: `expenses.plan_id` (money
  spent is real even if the plan is deleted), `meal_log.recipe_id`.

The trap this creates: `meal_log.member_id` is CASCADE, not RESTRICT. Intuition
says RESTRICT, so an accidental member deletion cannot erase a year of diary —
but `families → family_members` is CASCADE, and such a RESTRICT would break
family deletion with a confusing error. So **the API never hard-deletes a
member**: `DELETE /api/v1/members/{id}` sets `is_active = false`. Recipes work
the same way: `status = 'archived'`, never `DELETE`.
