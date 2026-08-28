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
    # TZ-M8: блюдо без национальной принадлежности (универсальная выпечка,
    # смузи, заготовки). Проходит любой жёсткий фильтр по кухне.
    "universal",
}
DISHES = {
    "soup", "salad", "appetizer", "sandwich", "steak", "main_course", "stew",
    "cutlets", "casserole", "porridge", "pasta", "pizza", "dumplings",
    "pancakes", "bread", "pie", "cake", "cookies", "dessert", "drink",
    "sauce", "preserves", "side",
}
LLM_CONFIDENCE = "0.8"


def _codes(item: dict) -> list[str]:
    """Кухни записи: волна мультиразметки шлёт список, первая волна — строку."""
    raw = item.get("cuisines")
    if raw is None:
        raw = [item["cuisine"]] if item.get("cuisine") else []
    return [str(code) for code in raw]


def _valid_cuisines(item: dict) -> bool:
    codes = _codes(item)
    return len(codes) <= 3 and all(code in CUISINES for code in codes)


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    updated = skipped_files = 0
    patterns = (
        "data/recipes/cuisines/multi/extracted/batch-*.json",
        "data/recipes/cuisines/extracted/batch-*.json",
    )
    for path in sorted(sum((glob.glob(pattern) for pattern in patterns), [])):
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
            or not _valid_cuisines(item)
            or (item.get("dish") is not None and item["dish"] not in DISHES)
        ]
        if bad:
            print(f"ПРОПУСК {os.path.basename(path)}: недопустимые записи {bad[:3]}")
            skipped_files += 1
            continue
        async with conn.transaction():
            for item in items:
                codes = _codes(item)
                # cuisine_code остаётся главным кодом (витрина, старые запросы),
                # cuisine_codes — полный набор; пусто значит «универсальное».
                main_code = next((code for code in codes if code != "universal"), None)
                result = await conn.execute(
                    """
                    UPDATE recipe_library.recipes
                    SET cuisine_code=$2::text,
                        cuisine_confidence=CASE WHEN $2::text IS NULL THEN NULL ELSE $3::numeric END,
                        dish_type=$4::text,
                        cuisine_codes=CASE
                            WHEN jsonb_array_length($5::jsonb) > 0 THEN $5::jsonb
                            ELSE '["universal"]'::jsonb
                        END
                    WHERE id=$1
                    """,
                    item["id"], main_code, LLM_CONFIDENCE, item.get("dish"),
                    json.dumps(codes, ensure_ascii=False),
                )
                updated += result.endswith(" 1")
    counts = await conn.fetchrow(
        """
        SELECT count(*) FILTER (WHERE cuisine_codes <> '[]'::jsonb) AS with_cuisine,
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
