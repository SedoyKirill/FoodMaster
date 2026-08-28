CREATE SCHEMA IF NOT EXISTS recipe_library;

CREATE TABLE IF NOT EXISTS recipe_library.sources (
    id BIGSERIAL PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    title TEXT,
    author TEXT,
    published_year INTEGER,
    language TEXT,
    page_count INTEGER NOT NULL,
    extraction_kind TEXT NOT NULL DEFAULT 'unknown',
    import_status TEXT NOT NULL DEFAULT 'pending',
    exclusion_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS recipe_library.source_pages (
    source_id BIGINT NOT NULL REFERENCES recipe_library.sources(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    extraction_method TEXT NOT NULL,
    text_content TEXT NOT NULL DEFAULT '',
    char_count INTEGER NOT NULL DEFAULT 0,
    text_sha256 TEXT,
    white_ratio NUMERIC,
    error TEXT,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_id, page_number)
);

CREATE TABLE IF NOT EXISTS recipe_library.recipes (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES recipe_library.sources(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    source_page_start INTEGER NOT NULL,
    source_page_end INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    source_servings_min NUMERIC,
    source_servings_max NUMERIC,
    source_yield_text TEXT,
    cuisine_code TEXT,
    cuisine_confidence NUMERIC,
    meal_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    diet_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    appliances JSONB NOT NULL DEFAULT '[]'::jsonb,
    extraction_confidence NUMERIC NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'needs_review',
    ingredient_count INTEGER NOT NULL DEFAULT 0,
    step_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_id, fingerprint),
    CHECK (review_status IN ('needs_review', 'ready', 'rejected'))
);

CREATE TABLE IF NOT EXISTS recipe_library.recipe_ingredients (
    recipe_id BIGINT NOT NULL REFERENCES recipe_library.recipes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    quantity_min NUMERIC,
    quantity_max NUMERIC,
    unit_raw TEXT,
    unit_code TEXT,
    ingredient_text TEXT NOT NULL,
    normalized_name TEXT,
    parsing_confidence NUMERIC NOT NULL,
    PRIMARY KEY (recipe_id, position)
);

CREATE TABLE IF NOT EXISTS recipe_library.recipe_steps (
    recipe_id BIGINT NOT NULL REFERENCES recipe_library.recipes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    instruction TEXT NOT NULL,
    PRIMARY KEY (recipe_id, position)
);

CREATE TABLE IF NOT EXISTS recipe_library.import_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    books_total INTEGER NOT NULL DEFAULT 0,
    books_completed INTEGER NOT NULL DEFAULT 0,
    books_excluded INTEGER NOT NULL DEFAULT 0,
    pages_native INTEGER NOT NULL DEFAULT 0,
    pages_ocr INTEGER NOT NULL DEFAULT 0,
    pages_image_only INTEGER NOT NULL DEFAULT 0,
    pages_error INTEGER NOT NULL DEFAULT 0,
    recipes_found INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb
);

ALTER TABLE recipe_library.sources ADD COLUMN IF NOT EXISTS exclusion_reason TEXT;
ALTER TABLE recipe_library.import_runs ADD COLUMN IF NOT EXISTS books_excluded INTEGER NOT NULL DEFAULT 0;

ALTER TABLE recipe_library.recipe_ingredients
    ADD COLUMN IF NOT EXISTS is_to_taste BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS section TEXT,
    ADD COLUMN IF NOT EXISTS note TEXT;

ALTER TABLE recipe_library.recipes
    ADD COLUMN IF NOT EXISTS time_total_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS extraction_method TEXT NOT NULL DEFAULT 'heuristic',
    ADD COLUMN IF NOT EXISTS review_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Разметка сессиями Claude по подписке (офлайн-этап): тип блюда
    -- (soup/salad/steak/…) для фильтров и планировщика.
    ADD COLUMN IF NOT EXISTS dish_type TEXT;

-- TZ-M8 (решение владельца 28.08.2026): кухня остаётся жёстким фильтром, а
-- значит должна быть у каждого рецепта — и не одна. Блюдо честно бывает
-- сразу русским и восточноевропейским, а универсальная выпечка не
-- принадлежит ни одной кухне и получает код 'universal', который проходит
-- любой фильтр. cuisine_code остаётся главным кодом для совместимости.
ALTER TABLE recipe_library.recipes
    ADD COLUMN IF NOT EXISTS cuisine_codes JSONB NOT NULL DEFAULT '[]'::jsonb;

UPDATE recipe_library.recipes
SET cuisine_codes = jsonb_build_array(cuisine_code)
WHERE cuisine_codes = '[]'::jsonb AND cuisine_code IS NOT NULL;

-- До волны мультиразметки блюдо без кухни считается универсальным: иначе
-- жёсткий фильтр молча выкинул бы четверть библиотеки.
UPDATE recipe_library.recipes
SET cuisine_codes = '["universal"]'::jsonb
WHERE cuisine_codes = '[]'::jsonb;

CREATE INDEX IF NOT EXISTS ix_recipes_cuisine_codes
    ON recipe_library.recipes USING GIN (cuisine_codes);

-- Справочник КБЖУ ингредиентов на 100 г (разметка Haiku-волнами,
-- загрузка scripts/load_nutrition.py). piece_mass_g — масса одной штуки.
CREATE TABLE IF NOT EXISTS recipe_library.ingredient_nutrition (
    name TEXT PRIMARY KEY,
    kcal_100 NUMERIC NOT NULL,
    protein_100 NUMERIC,
    fat_100 NUMERIC,
    carb_100 NUMERIC,
    piece_mass_g NUMERIC,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recipe_library.llm_extractions (
    window_sha256 TEXT NOT NULL,
    model TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    source_id BIGINT NOT NULL REFERENCES recipe_library.sources(id) ON DELETE CASCADE,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (window_sha256, model, schema_version)
);

CREATE INDEX IF NOT EXISTS ix_recipe_sources_status
    ON recipe_library.sources (import_status, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_recipe_pages_method
    ON recipe_library.source_pages (source_id, extraction_method, page_number);
CREATE INDEX IF NOT EXISTS ix_recipes_review
    ON recipe_library.recipes (review_status, extraction_confidence DESC);
CREATE INDEX IF NOT EXISTS ix_recipes_source_pages
    ON recipe_library.recipes (source_id, source_page_start);
