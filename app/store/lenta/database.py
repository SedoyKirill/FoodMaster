from __future__ import annotations

import json
from datetime import datetime
from importlib.resources import files
from typing import Any

import asyncpg

from .config import LentaConfig
from .models import ProductCard, ProductDetails


class LentaRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=4)
        schema = files("app.store.lenta").joinpath("schema.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as connection:
            await connection.execute(schema)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Repository is not connected")
        return self.pool

    async def upsert_store(self, config: LentaConfig) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            INSERT INTO lenta_store.stores (code, source, external_id, name, city, address)
            VALUES ($1, 'lenta', $2, 'Гипер Лента', $3, $4)
            ON CONFLICT (code) DO UPDATE SET
                external_id = EXCLUDED.external_id,
                city = EXCLUDED.city,
                address = EXCLUDED.address,
                updated_at = CURRENT_TIMESTAMP
            """,
            config.store_code,
            config.store_id,
            config.city,
            config.store_address,
        )

    async def begin_run(self, config: LentaConfig, started_at: datetime) -> int:
        pool = self._require_pool()
        return await pool.fetchval(
            """
            INSERT INTO lenta_store.collection_runs (source, store_code, started_at, status)
            VALUES ('lenta', $1, $2, 'running')
            RETURNING id
            """,
            config.store_code,
            started_at,
        )

    async def upsert_card(
        self,
        config: LentaConfig,
        card: ProductCard,
        observed_at: datetime,
    ) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            existing_id = await connection.fetchval(
                "SELECT id FROM lenta_store.store_products WHERE source = 'lenta' AND external_id = $1",
                card.external_id,
            )
            product_id = await connection.fetchval(
                """
                INSERT INTO lenta_store.store_products AS product (
                    source, external_id, name, url, image_url, pack_text,
                    pack_quantity, pack_unit, price_unit, last_seen_at
                )
                VALUES ('lenta', $1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (source, external_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    url = EXCLUDED.url,
                    image_url = COALESCE(EXCLUDED.image_url, product.image_url),
                    pack_text = COALESCE(EXCLUDED.pack_text, product.pack_text),
                    pack_quantity = COALESCE(EXCLUDED.pack_quantity, product.pack_quantity),
                    pack_unit = COALESCE(EXCLUDED.pack_unit, product.pack_unit),
                    price_unit = COALESCE(EXCLUDED.price_unit, product.price_unit),
                    last_seen_at = EXCLUDED.last_seen_at
                RETURNING id
                """,
                card.external_id,
                card.name,
                card.url,
                card.image_url,
                card.pack_text,
                card.pack_quantity,
                card.pack_unit,
                card.price_unit,
                observed_at,
            )
            if card.category_slug:
                await connection.execute(
                    """
                    INSERT INTO lenta_store.store_product_categories (product_id, category_slug)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    """,
                    product_id,
                    card.category_slug,
                )
            await connection.execute(
                """
                INSERT INTO lenta_store.store_listings (
                    product_id, store_code, available_for_order, purchase_limit,
                    raw_card_text, last_seen_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (product_id, store_code) DO UPDATE SET
                    available_for_order = EXCLUDED.available_for_order,
                    purchase_limit = EXCLUDED.purchase_limit,
                    raw_card_text = EXCLUDED.raw_card_text,
                    last_seen_at = EXCLUDED.last_seen_at
                """,
                product_id,
                config.store_code,
                card.available_for_order,
                card.purchase_limit,
                card.raw_text,
                observed_at,
            )
            await connection.execute(
                """
                INSERT INTO lenta_store.store_price_history (
                    product_id, store_code, observed_on, observed_at,
                    regular_price_kop, loyalty_price_kop, promo_price_kop,
                    personal_price_kop, discount_percent
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (product_id, store_code, observed_on) DO UPDATE SET
                    observed_at = EXCLUDED.observed_at,
                    regular_price_kop = EXCLUDED.regular_price_kop,
                    loyalty_price_kop = EXCLUDED.loyalty_price_kop,
                    promo_price_kop = EXCLUDED.promo_price_kop,
                    personal_price_kop = EXCLUDED.personal_price_kop,
                    discount_percent = EXCLUDED.discount_percent
                """,
                product_id,
                config.store_code,
                observed_at.date(),
                observed_at,
                card.regular_price_kop,
                card.loyalty_price_kop,
                card.promo_price_kop,
                card.personal_price_kop,
                card.discount_percent,
            )
        return existing_id is None

    async def save_details(self, external_id: str, details: ProductDetails, observed_at: datetime) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            UPDATE lenta_store.store_products SET
                name = COALESCE($2, name),
                article = $3,
                brand = $4,
                composition = $5,
                kcal_100 = $6,
                protein_100 = $7,
                fat_100 = $8,
                carb_100 = $9,
                storage_conditions = $10,
                shelf_life_text = $11,
                characteristics = $12::jsonb,
                details_updated_at = $13
            WHERE source = 'lenta' AND external_id = $1
            """,
            external_id,
            details.name,
            details.article,
            details.brand,
            details.composition,
            details.kcal_100,
            details.protein_100,
            details.fat_100,
            details.carb_100,
            details.storage_conditions,
            details.shelf_life_text,
            json.dumps(details.characteristics, ensure_ascii=False),
            observed_at,
        )

    async def detail_candidate_ids(self, config: LentaConfig, limit: int) -> list[str]:
        if limit <= 0:
            return []
        pool = self._require_pool()
        rows = await pool.fetch(
            """
            SELECT product.external_id
            FROM lenta_store.store_products AS product
            JOIN lenta_store.store_listings AS listing ON listing.product_id = product.id
            WHERE product.source = 'lenta'
              AND listing.store_code = $1
              AND listing.available_for_order
            ORDER BY product.details_updated_at ASC NULLS FIRST, product.last_seen_at DESC
            LIMIT $2
            """,
            config.store_code,
            limit,
        )
        return [str(row["external_id"]) for row in rows]

    async def mark_unseen_unavailable(self, config: LentaConfig, started_at: datetime) -> int:
        pool = self._require_pool()
        status = await pool.execute(
            """
            UPDATE lenta_store.store_listings
            SET available_for_order = FALSE
            WHERE store_code = $1
              AND last_seen_at < $2
              AND available_for_order
            """,
            config.store_code,
            started_at,
        )
        return int(status.rsplit(" ", 1)[-1])

    async def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        finished_at: datetime,
        pages_seen: int,
        products_seen: int,
        products_without_pack: int,
        details_enriched: int,
        errors: list[dict[str, Any]],
    ) -> None:
        pool = self._require_pool()
        await pool.execute(
            """
            UPDATE lenta_store.collection_runs SET
                finished_at = $2,
                status = $3,
                pages_seen = $4,
                products_seen = $5,
                products_without_pack = $6,
                details_enriched = $7,
                errors = $8::jsonb
            WHERE id = $1
            """,
            run_id,
            finished_at,
            status,
            pages_seen,
            products_seen,
            products_without_pack,
            details_enriched,
            json.dumps(errors, ensure_ascii=False),
        )
