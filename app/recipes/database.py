from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import asyncpg

from .models import ExtractedPage, RecipeCandidate


def recipe_fingerprint(candidate: RecipeCandidate) -> str:
    payload = (
        f"{candidate.page_start}:{candidate.page_end}:"
        f"{candidate.title.lower()}:{candidate.raw_text[:500]}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RecipeRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=3)
        schema = files("app.recipes").joinpath("schema.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as connection:
            await connection.execute(schema)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Recipe repository is not connected")
        return self.pool

    async def begin_run(self, books_total: int) -> int:
        return await self._pool().fetchval(
            """
            INSERT INTO recipe_library.import_runs (books_total)
            VALUES ($1)
            RETURNING id
            """,
            books_total,
        )

    async def upsert_source(
        self,
        *,
        sha256: str,
        file_name: str,
        file_path: str,
        file_size: int,
        title: str | None,
        author: str | None,
        published_year: int | None,
        language: str | None,
        page_count: int,
        metadata: dict[str, Any],
    ) -> tuple[int, str]:
        row = await self._pool().fetchrow(
            """
            INSERT INTO recipe_library.sources (
                sha256, file_name, file_path, file_size, title, author,
                published_year, language, page_count, metadata, import_status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, 'processing')
            ON CONFLICT (sha256) DO UPDATE SET
                file_name = EXCLUDED.file_name,
                file_path = EXCLUDED.file_path,
                file_size = EXCLUDED.file_size,
                title = COALESCE(EXCLUDED.title, recipe_library.sources.title),
                author = COALESCE(EXCLUDED.author, recipe_library.sources.author),
                published_year = COALESCE(EXCLUDED.published_year, recipe_library.sources.published_year),
                language = COALESCE(EXCLUDED.language, recipe_library.sources.language),
                page_count = EXCLUDED.page_count,
                metadata = EXCLUDED.metadata,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id, import_status
            """,
            sha256,
            file_name,
            file_path,
            file_size,
            title,
            author,
            published_year,
            language,
            page_count,
            json.dumps(metadata, ensure_ascii=False),
        )
        return int(row["id"]), str(row["import_status"])

    async def reset_source(self, source_id: int) -> None:
        async with self._pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM recipe_library.source_pages WHERE source_id = $1", source_id
            )
            await connection.execute(
                "DELETE FROM recipe_library.recipes WHERE source_id = $1", source_id
            )
            await connection.execute(
                """
                UPDATE recipe_library.sources
                SET import_status = 'processing', exclusion_reason = NULL,
                    completed_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                source_id,
            )

    async def processed_pages(self, source_id: int) -> set[int]:
        rows = await self._pool().fetch(
            """
            SELECT page_number
            FROM recipe_library.source_pages
            WHERE source_id = $1 AND extraction_method <> 'error'
            """,
            source_id,
        )
        return {int(row["page_number"]) for row in rows}

    async def save_page(self, source_id: int, page: ExtractedPage) -> None:
        text_hash = hashlib.sha256(page.text.encode("utf-8")).hexdigest() if page.text else None
        await self._pool().execute(
            """
            INSERT INTO recipe_library.source_pages (
                source_id, page_number, extraction_method, text_content,
                char_count, text_sha256, white_ratio, error
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (source_id, page_number) DO UPDATE SET
                extraction_method = EXCLUDED.extraction_method,
                text_content = EXCLUDED.text_content,
                char_count = EXCLUDED.char_count,
                text_sha256 = EXCLUDED.text_sha256,
                white_ratio = EXCLUDED.white_ratio,
                error = EXCLUDED.error,
                processed_at = CURRENT_TIMESTAMP
            """,
            source_id,
            page.page_number,
            page.method,
            page.text,
            len(page.text),
            text_hash,
            page.white_ratio,
            page.error,
        )

    async def load_pages(self, source_id: int) -> list[ExtractedPage]:
        rows = await self._pool().fetch(
            """
            SELECT page_number, extraction_method, text_content, white_ratio, error
            FROM recipe_library.source_pages
            WHERE source_id = $1
            ORDER BY page_number
            """,
            source_id,
        )
        return [
            ExtractedPage(
                page_number=int(row["page_number"]),
                method=str(row["extraction_method"]),
                text=str(row["text_content"] or ""),
                white_ratio=row["white_ratio"],
                error=row["error"],
            )
            for row in rows
        ]

    async def replace_recipes(self, source_id: int, recipes: list[RecipeCandidate]) -> None:
        async with self._pool().acquire() as connection, connection.transaction():
            # Планы, построенные на заменяемых рецептах, теряют смысл —
            # снимаем ссылки, иначе замена невозможна (FK plan_meals.recipe_id).
            has_plan_meals = await connection.fetchval(
                "SELECT to_regclass('app_core.plan_meals') IS NOT NULL"
            )
            if has_plan_meals:
                await connection.execute(
                    """
                    DELETE FROM app_core.plan_meals WHERE recipe_id IN (
                        SELECT id FROM recipe_library.recipes WHERE source_id = $1
                    )
                    """,
                    source_id,
                )
            await connection.execute(
                "DELETE FROM recipe_library.recipes WHERE source_id = $1", source_id
            )
            for recipe in recipes:
                recipe_id = await connection.fetchval(
                    """
                    INSERT INTO recipe_library.recipes (
                        source_id, fingerprint, title, source_page_start, source_page_end,
                        raw_text, source_servings_min, source_servings_max, source_yield_text,
                        cuisine_code, cuisine_confidence, meal_types, diet_tags, appliances,
                        extraction_confidence, review_status, ingredient_count, step_count,
                        time_total_minutes, extraction_method, review_reasons
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12::jsonb, $13::jsonb, $14::jsonb, $15, $18, $16, $17,
                        $19, $20, $21::jsonb
                    )
                    RETURNING id
                    """,
                    source_id,
                    recipe_fingerprint(recipe),
                    recipe.title,
                    recipe.page_start,
                    recipe.page_end,
                    recipe.raw_text,
                    recipe.source_servings_min,
                    recipe.source_servings_max,
                    recipe.source_yield_text,
                    recipe.cuisine_code,
                    recipe.cuisine_confidence,
                    json.dumps(recipe.meal_types, ensure_ascii=False),
                    json.dumps(recipe.diet_tags, ensure_ascii=False),
                    json.dumps(recipe.appliances, ensure_ascii=False),
                    recipe.confidence,
                    len(recipe.ingredients),
                    len(recipe.steps),
                    recipe.review_status,
                    recipe.time_total_minutes,
                    recipe.extraction_method,
                    json.dumps(recipe.review_reasons, ensure_ascii=False),
                )
                await connection.executemany(
                    """
                    INSERT INTO recipe_library.recipe_ingredients (
                        recipe_id, position, raw_text, quantity_min, quantity_max,
                        unit_raw, unit_code, ingredient_text, normalized_name,
                        parsing_confidence, is_to_taste, section, note
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """,
                    [
                        (
                            recipe_id,
                            index,
                            ingredient.raw_text,
                            ingredient.quantity_min,
                            ingredient.quantity_max,
                            ingredient.unit_raw,
                            ingredient.unit_code,
                            ingredient.ingredient_text,
                            ingredient.normalized_name,
                            ingredient.confidence,
                            ingredient.is_to_taste,
                            ingredient.section,
                            ingredient.note,
                        )
                        for index, ingredient in enumerate(recipe.ingredients, 1)
                    ],
                )
                await connection.executemany(
                    """
                    INSERT INTO recipe_library.recipe_steps (recipe_id, position, instruction)
                    VALUES ($1, $2, $3)
                    """,
                    [
                        (recipe_id, index, instruction)
                        for index, instruction in enumerate(recipe.steps, 1)
                    ],
                )

    async def finish_source(
        self,
        source_id: int,
        extraction_kind: str,
        status: str,
        exclusion_reason: str | None = None,
    ) -> None:
        await self._pool().execute(
            """
            UPDATE recipe_library.sources SET
                extraction_kind = $2,
                import_status = $3,
                exclusion_reason = $4,
                updated_at = CURRENT_TIMESTAMP,
                completed_at = CASE WHEN $3 = 'completed' THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id = $1
            """,
            source_id,
            extraction_kind,
            status,
            exclusion_reason,
        )

    async def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        metrics: dict[str, int],
        errors: list[dict[str, Any]],
    ) -> None:
        await self._pool().execute(
            """
            UPDATE recipe_library.import_runs SET
                finished_at = $2,
                status = $3,
                books_completed = $4,
                books_excluded = $5,
                pages_native = $6,
                pages_ocr = $7,
                pages_image_only = $8,
                pages_error = $9,
                recipes_found = $10,
                errors = $11::jsonb
            WHERE id = $1
            """,
            run_id,
            datetime.now(UTC),
            status,
            metrics.get("books_completed", 0),
            metrics.get("books_excluded", 0),
            metrics.get("pages_native", 0),
            metrics.get("pages_ocr", 0),
            metrics.get("pages_image_only", 0),
            metrics.get("pages_error", 0),
            metrics.get("recipes_found", 0),
            json.dumps(errors, ensure_ascii=False),
        )

    async def library_summary(self) -> dict[str, Any]:
        row = await self._pool().fetchrow(
            """
            SELECT
                (SELECT count(*) FROM recipe_library.sources) AS books,
                (SELECT count(*) FROM recipe_library.sources WHERE import_status = 'excluded') AS excluded_books,
                (SELECT count(*) FROM recipe_library.source_pages) AS pages,
                (SELECT count(*) FROM recipe_library.source_pages WHERE extraction_method = 'ocr') AS ocr_pages,
                (SELECT count(*) FROM recipe_library.recipes) AS recipes,
                (SELECT count(*) FROM recipe_library.recipe_ingredients) AS ingredients,
                (SELECT count(*) FROM recipe_library.recipes WHERE source_servings_min IS NOT NULL) AS with_servings,
                (SELECT count(*) FROM recipe_library.recipes WHERE review_status = 'ready') AS ready_recipes,
                (SELECT count(*) FROM recipe_library.llm_extractions) AS llm_extractions
            """
        )
        return {key: int(value or 0) for key, value in dict(row).items()}

    async def page_methods(self, source_id: int) -> set[str]:
        rows = await self._pool().fetch(
            "SELECT DISTINCT extraction_method FROM recipe_library.source_pages WHERE source_id = $1",
            source_id,
        )
        return {str(row["extraction_method"]) for row in rows}

    async def sources_index(self) -> dict[int, dict]:
        rows = await self._pool().fetch(
            "SELECT id, title, file_name FROM recipe_library.sources ORDER BY id"
        )
        return {
            int(row["id"]): {"title": row["title"], "file_name": row["file_name"]}
            for row in rows
        }

    async def save_llm_extraction(
        self,
        *,
        window_sha256: str,
        model: str,
        schema_version: int,
        source_id: int,
        page_start: int,
        page_end: int,
        response: dict[str, Any],
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO recipe_library.llm_extractions (
                window_sha256, model, schema_version, source_id,
                page_start, page_end, response
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT (window_sha256, model, schema_version) DO UPDATE SET
                response = EXCLUDED.response,
                page_start = EXCLUDED.page_start,
                page_end = EXCLUDED.page_end
            """,
            window_sha256,
            model,
            schema_version,
            source_id,
            page_start,
            page_end,
            json.dumps(response, ensure_ascii=False),
        )
