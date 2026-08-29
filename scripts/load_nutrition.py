"""Загрузка справочника КБЖУ из data/nutrition/extracted (разметка Haiku).

Запуск: DATABASE_URL=postgresql://ration:ration@localhost:5432/ration \
    python scripts/load_nutrition.py
Идемпотентно: UPSERT по имени. Невалидные записи отбрасываются с логом:
kcal вне [0..900], БЖУ вне [0..100], либо энергия из БЖУ существенно
превышает заявленную ккал (перекачанные макросы). Недобор энергии допустим —
алкоголь и органические кислоты дают ккал вне БЖУ.
"""

import asyncio
import glob
import json
import os
from decimal import Decimal

import asyncpg


def valid_row(item: dict) -> str | None:
    """None — запись валидна, иначе причина отбраковки."""
    name = str(item.get("name") or "").strip().lower()
    if not name or len(name) > 80:
        return "пустое или слишком длинное имя"
    try:
        kcal = float(item["kcal_100"])
        protein = float(item.get("protein_100") or 0)
        fat = float(item.get("fat_100") or 0)
        carb = float(item.get("carb_100") or 0)
    except (KeyError, TypeError, ValueError):
        return "не числа"
    if not (0 <= kcal <= 900):
        return f"kcal {kcal} вне [0..900]"
    for label, value in (("Б", protein), ("Ж", fat), ("У", carb)):
        if not (0 <= value <= 100):
            return f"{label} {value} вне [0..100]"
    # Углеводы считаются по 2 ккал/г, а не по 4: у специй, отрубей и
    # разрыхлителя основная часть «углеводов» — клетчатка и карбонаты, которые
    # организм так не усваивает. С коэффициентом 4 проверка отбраковывала
    # настоящие справочные значения (душистый перец 263 ккал при 72 г
    # углеводов, разрыхлитель 53 при 28) — то есть выбрасывала все специи
    # разом. Колонки клетчатки в справочнике нет, поэтому берётся усреднение.
    computed = 4 * protein + 9 * fat + 2 * carb
    if computed > kcal * 1.35 + 30:
        return f"БЖУ дают {computed:.0f} ккал при заявленных {kcal:.0f}"
    piece = item.get("piece_mass_g")
    if piece is not None:
        # Масса штуки вне разумных границ (арбуз на 5 кг) — повод выбросить
        # само поле, а не всю запись: КБЖУ арбуза остаётся верным, теряется
        # только пересчёт «2 штуки» в граммы.
        try:
            if not (0 < float(piece) <= 1000):
                item["piece_mass_g"] = None
        except (TypeError, ValueError):
            item["piece_mass_g"] = None
    return None


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    loaded = rejected = 0
    # Пачки первой разметки лежат как batch-*.json, ответы волны TZ-M8 —
    # как wave-batch-*.json. Загрузка идемпотентна, так что порядок не важен.
    paths = sorted(
        glob.glob("data/nutrition/extracted/batch-*.json")
        + glob.glob("data/nutrition/extracted/wave-batch-*.json")
    )
    for path in paths:
        try:
            items = json.loads(open(path, encoding="utf-8-sig").read())
            assert isinstance(items, list)
        except Exception as exc:  # noqa: BLE001
            print(f"ПРОПУСК {os.path.basename(path)}: {exc}")
            continue
        async with conn.transaction():
            for item in items:
                reason = valid_row(item)
                if reason:
                    rejected += 1
                    print(f"ОТБРОШЕНО {item.get('name')!r}: {reason}")
                    continue
                await conn.execute(
                    """
                    INSERT INTO recipe_library.ingredient_nutrition
                        (name, kcal_100, protein_100, fat_100, carb_100, piece_mass_g)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (name) DO UPDATE SET
                        kcal_100=EXCLUDED.kcal_100, protein_100=EXCLUDED.protein_100,
                        fat_100=EXCLUDED.fat_100, carb_100=EXCLUDED.carb_100,
                        piece_mass_g=EXCLUDED.piece_mass_g,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    str(item["name"]).strip().lower(),
                    Decimal(str(item["kcal_100"])),
                    Decimal(str(item.get("protein_100") or 0)),
                    Decimal(str(item.get("fat_100") or 0)),
                    Decimal(str(item.get("carb_100") or 0)),
                    Decimal(str(item["piece_mass_g"])) if item.get("piece_mass_g") is not None else None,
                )
                loaded += 1
    total = await conn.fetchval("SELECT count(*) FROM recipe_library.ingredient_nutrition")
    print(f"загружено/обновлено: {loaded}; отброшено: {rejected}; всего в таблице: {total}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
