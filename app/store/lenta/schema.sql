CREATE SCHEMA IF NOT EXISTS lenta_store;

CREATE TABLE IF NOT EXISTS lenta_store.stores (
    code TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    address TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS lenta_store.store_products (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    image_url TEXT,
    pack_text TEXT,
    pack_quantity NUMERIC,
    pack_unit TEXT,
    price_unit TEXT,
    article TEXT,
    brand TEXT,
    composition TEXT,
    kcal_100 NUMERIC,
    protein_100 NUMERIC,
    fat_100 NUMERIC,
    carb_100 NUMERIC,
    storage_conditions TEXT,
    shelf_life_text TEXT,
    characteristics JSONB NOT NULL DEFAULT '{}'::jsonb,
    details_updated_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS lenta_store.store_product_categories (
    product_id BIGINT NOT NULL REFERENCES lenta_store.store_products(id) ON DELETE CASCADE,
    category_slug TEXT NOT NULL,
    PRIMARY KEY (product_id, category_slug)
);

CREATE TABLE IF NOT EXISTS lenta_store.store_listings (
    product_id BIGINT NOT NULL REFERENCES lenta_store.store_products(id) ON DELETE CASCADE,
    store_code TEXT NOT NULL REFERENCES lenta_store.stores(code) ON DELETE CASCADE,
    available_for_order BOOLEAN NOT NULL,
    purchase_limit INTEGER,
    raw_card_text TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (product_id, store_code)
);

CREATE TABLE IF NOT EXISTS lenta_store.store_price_history (
    product_id BIGINT NOT NULL REFERENCES lenta_store.store_products(id) ON DELETE CASCADE,
    store_code TEXT NOT NULL REFERENCES lenta_store.stores(code) ON DELETE CASCADE,
    observed_on DATE NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    regular_price_kop INTEGER,
    loyalty_price_kop INTEGER,
    promo_price_kop INTEGER,
    personal_price_kop INTEGER,
    discount_percent INTEGER,
    PRIMARY KEY (product_id, store_code, observed_on)
);

-- Совместимое обновление уже созданного локального тома PostgreSQL.
ALTER TABLE lenta_store.store_products ADD COLUMN IF NOT EXISTS article TEXT;
ALTER TABLE lenta_store.store_price_history ADD COLUMN IF NOT EXISTS promo_price_kop INTEGER;

CREATE TABLE IF NOT EXISTS lenta_store.collection_runs (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    store_code TEXT NOT NULL REFERENCES lenta_store.stores(code),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    pages_seen INTEGER NOT NULL DEFAULT 0,
    products_seen INTEGER NOT NULL DEFAULT 0,
    products_without_pack INTEGER NOT NULL DEFAULT 0,
    details_enriched INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS ix_store_products_last_seen
    ON lenta_store.store_products (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS ix_store_price_history_observed
    ON lenta_store.store_price_history (store_code, observed_on DESC);
CREATE INDEX IF NOT EXISTS ix_store_listings_available
    ON lenta_store.store_listings (store_code, available_for_order, last_seen_at DESC);
