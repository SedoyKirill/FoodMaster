"""Пачки рецептов для волны мультиразметки кухонь (TZ-M8, решение 28.08.2026).

Кухня осталась жёстким фильтром, поэтому она должна быть у каждого рецепта —
и не одна: борщ честно и русский, и восточноевропейский, а универсальная
выпечка получает код ``universal``. Первая волна (август 2026) ставила ровно
одну кухню и оставляла ``null`` — теперь размечается вся библиотека заново.

Запуск:
    docker compose --profile manual run --rm -T recipe-importer \
        python scripts/build_cuisine_batches.py

Пишет ``data/recipes/cuisines/multi/batch-NNN.md`` по 120 строк. Ответы
складываются в ``data/recipes/cuisines/multi/extracted/batch-NNN.json`` и
загружаются ``scripts/load_cuisines.py``.
"""

import asyncio
import os

import asyncpg

BATCH_SIZE = 120
OUTPUT_DIR = "data/recipes/cuisines/multi"
#: сколько ингредиентов показывать в подсказке — этого хватает для кухни
INGREDIENT_HINT_LIMIT = 8


async def main() -> None:
    only_untagged = os.getenv("CUISINE_ONLY_UNTAGGED", "false").lower() == "true"
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await conn.fetch(
        f"""
        SELECT r.id, r.title, s.title AS book, r.cuisine_code,
               (SELECT string_agg(i.normalized_name, ', ' ORDER BY i.position)
                FROM (
                    SELECT normalized_name, position
                    FROM recipe_library.recipe_ingredients
                    WHERE recipe_id = r.id
                    ORDER BY position
                    LIMIT {INGREDIENT_HINT_LIMIT}
                ) i) AS ingredients
        FROM recipe_library.recipes r
        JOIN recipe_library.sources s ON s.id = r.source_id
        WHERE r.review_status <> 'rejected'
          {"AND r.cuisine_code IS NULL" if only_untagged else ""}
        ORDER BY r.id
        """
    )
    await conn.close()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for index in range(0, len(rows), BATCH_SIZE):
        chunk = rows[index : index + BATCH_SIZE]
        number = index // BATCH_SIZE + 1
        path = os.path.join(OUTPUT_DIR, f"batch-{number:03d}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"# batch-{number:03d}\n")
            handle.write("id | название | книга | текущая кухня | ингредиенты\n")
            for row in chunk:
                handle.write(
                    f"{row['id']} | {row['title']} | {row['book']} | "
                    f"{row['cuisine_code'] or '—'} | {row['ingredients'] or '—'}\n"
                )
    print(f"рецептов: {len(rows)}; пачек: {-(-len(rows) // BATCH_SIZE)} в {OUTPUT_DIR}")


asyncio.run(main())
