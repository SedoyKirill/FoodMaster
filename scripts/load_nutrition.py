"""Загрузка справочника КБЖУ из data/nutrition/extracted (разметка Haiku).

Запуск: DATABASE_URL=postgresql://ration:ration@localhost:5432/ration \
    python scripts/load_nutrition.py
Идемпотентно: UPSERT по имени. Невалидные записи отбрасываются с логом:
kcal вне [0..900], БЖУ вне [0..100], либо энергия из БЖУ (4Б+9Ж+4У)
существенно превышает заявленную ккал (перекачанные макросы). Недобор
энергии допустим — алкоголь и органические кислоты дают ккал вне БЖУ.
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
    computed = 4 * protein + 9 * fat + 4 * carb
    if computed > kcal * 1.35 + 30:
        return f"БЖУ дают {computed:.0f} ккал при заявленных {kcal:.0f}"
    piece = item.get("piece_mass_g")
    if piece is not None:
        try:
            if not (0 < float(piece) <= 1000):
                return f"piece_mass_g {piece} вне (0..1000]"
        except (TypeError, ValueError):
            return "piece_mass_g не число"
    return None


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    loaded = rejected = 0
    for path in sorted(glob.glob("data/nutrition/extracted/batch-*.json")):
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
