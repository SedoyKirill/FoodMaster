# ТЗ M1 — Ядро: база данных, каркас приложения

## Цель
Фундамент для всех остальных модулей: схема БД, подключение, конфигурация,
каркас FastAPI, docker-окружение. После M1 проект запускается, миграции применяются,
`/health` отвечает.

## Зависимости
Нет. Первый модуль.

## Состав работ

### 1. Окружение — всё в Docker
Требование пользователя: система включается `docker compose up -d` и
выключается `docker compose down`, ничего кроме Docker Desktop на хост не ставим.

- `Dockerfile` — multi-stage: стадия сборки фронтенда (Node, добавляется в M6)
  и Python-образ приложения (используется сервисами api и scheduler с разными
  командами запуска). В M1 — только Python-стадия.
- `docker-compose.yml`, сервисы:
  - `db` — `pgvector/pgvector:pg16`, named volume `pgdata`, healthcheck.
  - `ollama` — `ollama/ollama`, volume `ollama_models`, проброс GPU:
    `deploy.resources.reservations.devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]`.
    Init-скрипт/entrypoint делает `ollama pull qwen3:8b` при первом старте.
  - `api` — FastAPI (uvicorn), depends_on db (healthy); entrypoint сначала
    выполняет `alembic upgrade head`, потом стартует сервер. Публикация порта:
    по умолчанию `127.0.0.1:8000:8000` (только этот ПК); при `WEB_EXPOSE=1` —
    `8000:8000` (доступ с телефонов по домашней сети).
  - `scheduler` — APScheduler-процесс (ночной парсинг цен M3, ночной бэкап БД),
    depends_on db.
- Общая сеть compose; сервисы ходят друг к другу по именам (`db`, `ollama`).
- Кеш модели эмбеддингов (sentence-transformers) — в volume `hf_cache`,
  чтобы не скачивалась при каждом пересоздании контейнера.
- `pyproject.toml`: fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic,
  pydantic-settings, apscheduler, httpx, sentence-transformers, ortools, pytest.
- `.env.example`: `DATABASE_URL`, `LLM_PROVIDER=none|ollama|claude`,
  `OLLAMA_URL`, `OLLAMA_MODEL=qwen3:8b`, `ANTHROPIC_API_KEY` (опц.),
  `STORES=5ka,lenta`, `FIVEKA_STORE_ID`, `LENTA_STORE_ID`, `WEB_EXPOSE=0`,
  `BACKUP_DIR=` (опц., путь к смонтированной SMB-шаре NAS).
- `app/core/config.py`: pydantic-settings, читает `.env`.
- `app/core/db.py`: async engine, session factory, `get_session` dependency.

### 2. Схема данных (Alembic-миграция №1)

Все таблицы проекта создаются здесь, чтобы модули не конфликтовали миграциями.
Авторизации в системе нет (приложение живёт в локальной сети), поэтому таблицы
пользователей нет: семья одна, создаётся мастером первого запуска (M6).

```sql
-- Семья
families         (id PK, name,
                  month_budget_kop INT NULL,    -- месячный бюджет на еду (M7)
                  created_at)
family_members   (id PK, family_id FK, name, sex, birth_date,
                  weight_kg NUMERIC, height_cm NUMERIC,
                  activity_level TEXT,          -- sedentary|light|moderate|high
                  kcal_override INT NULL,       -- ручное переопределение нормы
                  restrictions JSONB)           -- аллергии, "не ест рыбу" и т.п.
weight_log       (member_id FK, date DATE, weight_kg NUMERIC,
                  PRIMARY KEY(member_id, date)) -- история веса (M7 наполняет)

-- Рецепты (M2 наполняет)
recipes          (id PK, title, source_url, description,
                  meal_type TEXT[],             -- breakfast|lunch|dinner|snack
                  cook_method TEXT,             -- boil|fry|bake|stew|raw
                  cook_minutes INT NULL,        -- общее время приготовления
                  steps JSONB, tags TEXT[],
                  servings INT,                 -- на сколько порций граммовки
                  nutrition_incomplete BOOLEAN NOT NULL DEFAULT FALSE,
                                                -- M4: есть значимый ингредиент без КБЖУ
                  embedding VECTOR(384),
                  created_at)
ingredients      (id PK, name TEXT UNIQUE,      -- нормализованное имя: "куриное филе"
                  category TEXT,                -- мясо|овощи|крупы|молочка|...
                  kcal_100 NUMERIC, protein_100 NUMERIC,
                  fat_100 NUMERIC, carb_100 NUMERIC,   -- на 100 г СЫРОГО
                  embedding VECTOR(384))
recipe_ingredients (recipe_id FK, ingredient_id FK, grams NUMERIC,
                    note TEXT, PRIMARY KEY(recipe_id, ingredient_id))

-- Коэффициенты термообработки (M4 наполняет)
cook_coefficients (ingredient_category TEXT, cook_method TEXT,
                   weight_factor NUMERIC,      -- 0.65 = ужарка мяса на 35%
                   PRIMARY KEY(ingredient_category, cook_method))

-- Товары магазина (M3 наполняет)
store_products   (id PK, external_id TEXT, store TEXT DEFAULT '5ka',
                  name TEXT, category TEXT,
                  pack_grams NUMERIC,           -- вес/объём упаковки
                  ingredient_id FK NULL,        -- результат матчинга
                  match_confidence NUMERIC,
                  embedding VECTOR(384),
                  UNIQUE(store, external_id))
price_history    (product_id FK, price_kop INT, promo BOOLEAN,
                  observed_at DATE, PRIMARY KEY(product_id, observed_at))
parse_runs       (id PK, store TEXT DEFAULT '5ka',
                  started_at, finished_at NULL,
                  status TEXT,                  -- running|ok|failed
                  stats JSONB)                  -- товаров, новых, без граммовки, ошибок

-- Планы (M5 наполняет)
meal_plans       (id PK, family_id FK, days INT, status TEXT,  -- draft|active|done
                  params JSONB,                 -- бюджет, исключения, магазин, пожелания
                  created_at)
plan_meals       (id PK, plan_id FK, day INT, meal_type TEXT,
                  recipe_id FK, portions JSONB) -- {member_id: множитель порции}
shopping_items   (id PK, plan_id FK, product_id FK, qty INT,
                  planned_price_kop INT, bought BOOLEAN DEFAULT FALSE,
                  actual_price_kop INT NULL)
fridge           (id PK, family_id FK, ingredient_id FK, grams NUMERIC,
                  expires_at DATE NULL)         -- виртуальный холодильник

-- Дневник и финансы (M7 наполняет)
meal_log         (id PK, member_id FK, date DATE, meal_type TEXT,
                  recipe_id FK NULL, eaten_fraction NUMERIC DEFAULT 1.0,
                  free_text TEXT NULL,          -- перекус не по плану
                  kcal NUMERIC, protein NUMERIC, fat NUMERIC, carb NUMERIC)
expenses         (id PK, family_id FK, date DATE, amount_kop INT,
                  source TEXT,                  -- plan|manual|receipt
                  plan_id FK NULL, note TEXT)
```

Индексы: HNSW по всем `embedding` (`vector_cosine_ops`),
`price_history(observed_at)`, `meal_log(member_id, date)`, `parse_runs(started_at)`.

### 3. Каркас FastAPI
- `app/main.py`: создание приложения, подключение роутеров модулей
  (пока пустых, все под префиксом `/api/v1`), `GET /health` →
  `{status, db: ok, ollama: ok|absent}`.
- Единый обработчик ошибок (формат `{error: {code, message}}`),
  структурное логирование (structlog или logging+json).
- Раздача статики SPA с fallback на `index.html` добавляется в M6;
  в M1 корень `/` отдаёт простую заглушку со ссылкой на `/docs`.

### 4. Запуск
- Продакшен-режим: только `docker compose up -d` / `down`.
- Dev-режим: `scripts/dev.ps1` — поднять db+ollama в докере, приложение
  локально с hot-reload (для разработки).
- Опционально: Docker Desktop умеет автозапуск при входе в Windows +
  `restart: unless-stopped` у сервисов — система сама поднимется после ребута.

### 5. Бэкапы БД
- Ежедневное задание в scheduler: `pg_dump` (custom format) в volume `backups`;
  если задан `BACKUP_DIR` (смонтированная SMB-шара NAS) — дамп копируется туда.
  Ротация: хранить последние 14 дампов в обоих местах.
- `scripts/restore.ps1 <файл>` — восстановление дампа в чистую БД.
- ВАЖНО: data-каталог Postgres — только на локальном диске (volume);
  на сетевой шаре его размещать нельзя (блокировки сетевых ФС ненадёжны).

## Критерии готовности
- [ ] `docker compose up -d` поднимает все сервисы; `docker compose down` +
      повторный `up` не теряет данные (volumes).
- [ ] Ollama в контейнере видит GPU (`docker compose exec ollama nvidia-smi`),
      `qwen3:8b` отвечает на тестовый промпт.
- [ ] Миграции применяются автоматически при старте api.
- [ ] `GET /health` возвращает 200 со статусом db и ollama.
- [ ] В БД созданы все таблицы, HNSW-индексы построены.
- [ ] pytest-заготовка работает, есть фикстура тестовой БД.
- [ ] `.env.example` полный, README-раздел «Быстрый старт» воспроизводится с нуля.
- [ ] Ночной бэкап создаёт дамп; при заданном `BACKUP_DIR` копия появляется
      на NAS; `restore.ps1` восстанавливает дамп в пустую БД (проверено один раз).
