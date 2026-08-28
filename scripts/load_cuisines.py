"""Загрузка разметки кухонь/типов блюд из data/recipes/cuisines/extracted.

Запуск: docker compose --profile manual run --rm -T recipe-importer \
    python scripts/load_cuisines.py
Идемпотентно: UPDATE по id, неизвестные коды отбрасывают файл целиком.
"""

import asyncio
import glob
import json
import os

import asyncpg

CUISINES = {
    "russian", "east_european", "italian", "french", "georgian",
    "mediterranean", "middle_eastern", "asian", "japanese", "indian",
    "mexican", "american",
}
DISHES = {
    "soup", "salad", "appetizer", "sandwich", "steak", "main_course", "stew",
    "cutlets", "casserole", "porridge", "pasta", "pizza", "dumplings",
    "pancakes", "bread", "pie", "cake", "cookies", "dessert", "drink",
    "sauce", "preserves", "side",
}
LLM_CONFIDENCE = "0.8"


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    updated = skipped_files = 0
    for path in sorted(glob.glob("data/recipes/cuisines/extracted/batch-*.json")):
        try:
            data = json.loads(open(path, encoding="utf-8-sig").read())
            items = data["items"]
            assert isinstance(items, list)
        except Exception as exc:  # noqa: BLE001
            print(f"ПРОПУСК {os.path.basename(path)}: {exc}")
            skipped_files += 1
            continue
        bad = [
            item for item in items
            if not isinstance(item.get("id"), int)
            or (item.get("cuisine") is not None and item["cuisine"] not in CUISINES)
            or (item.get("dish") is not None and item["dish"] not in DISHES)
        ]
        if bad:
            print(f"ПРОПУСК {os.path.basename(path)}: недопустимые записи {bad[:3]}")
            skipped_files += 1
            continue
        async with conn.transaction():
            for item in items:
                result = await conn.execute(
                    """
                    UPDATE recipe_library.recipes
                    SET cuisine_code=$2::text,
                        cuisine_confidence=CASE WHEN $2::text IS NULL THEN NULL ELSE $3::numeric END,
                        dish_type=$4::text
                    WHERE id=$1
                    """,
                    item["id"], item.get("cuisine"), LLM_CONFIDENCE, item.get("dish"),
                )
                updated += result.endswith(" 1")
    counts = await conn.fetchrow(
        """
        SELECT count(*) FILTER (WHERE cuisine_code IS NOT NULL) AS with_cuisine,
               count(*) FILTER (WHERE dish_type IS NOT NULL) AS with_dish,
               count(*) AS total
        FROM recipe_library.recipes WHERE review_status <> 'rejected'
        """
    )
    print(
        f"обновлено строк: {updated}; пропущено файлов: {skipped_files}; "
        f"в БД: кухня у {counts['with_cuisine']}, тип у {counts['with_dish']} "
        f"из {counts['total']}"
    )
    await conn.close()


asyncio.run(main())
