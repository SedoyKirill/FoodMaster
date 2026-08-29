"""Пачки ингредиентов для волны разметки КБЖУ (TZ-M8 §9.2, отчёт 28.08.2026).

Справочник ``recipe_library.ingredient_nutrition`` покрывает 262 канонических
имени из 1687 — по упоминаниям это 70 %, но у половины блюд белок остаётся
неизвестным, и критерий приёмки ``protein_gap`` меряет полноту разметки, а не
питание.

Ключ справочника — **каноническое имя**, то самое, по которому планировщик
ищет в ``_make_macros_hint``: словоформы приводятся словарём синонимов. Поэтому
и здесь имена канонизируются тем же кодом, а не берутся из базы как есть —
иначе разметка легла бы мимо ключей поиска.

Пачки идут по убыванию частоты: если волна оборвётся, сделанной окажется самая
полезная часть. Рядом с каждым именем — как оно пишется в книгах: «мука
ржаная» понятнее в компании «муки ржаной обдирной».

Запуск:
    DATABASE_URL=postgresql://ration:ration@127.0.0.1:5432/ration \\
        python scripts/build_nutrition_batches.py

Пишет ``data/nutrition/wave/batch-NNN.md``; ответы складываются в
``data/nutrition/extracted/`` и загружаются ``scripts/load_nutrition.py``.
"""

import asyncio
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402

from app.web.planner import _normal  # noqa: E402
from app.web.planning.candidates import Synonyms  # noqa: E402

BATCH_SIZE = int(os.getenv("NUTRITION_BATCH_SIZE", "120"))
OUTPUT_DIR = "data/nutrition/wave"
#: сколько вариантов написания показывать рядом с каноническим именем
SPELLING_HINT_LIMIT = 4


async def collect() -> list[tuple[str, int, list[str]]]:
    """(каноническое имя, число упоминаний, как пишется) — только недостающие."""
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        synonyms = Synonyms.from_rows([
            dict(row) for row in await conn.fetch(
                "SELECT term, canonical, kind FROM app_core.ingredient_synonyms"
            )
        ])
        known = {
            str(row["name"]).strip().lower()
            for row in await conn.fetch(
                "SELECT name FROM recipe_library.ingredient_nutrition"
            )
        }
        rows = await conn.fetch(
            """
            SELECT i.normalized_name AS name, count(*) AS uses
            FROM recipe_library.recipe_ingredients i
            JOIN recipe_library.recipes r ON r.id = i.recipe_id
            WHERE r.review_status = 'ready'
              AND i.normalized_name IS NOT NULL AND i.normalized_name <> ''
            GROUP BY i.normalized_name
            """
        )
    finally:
        await conn.close()

    uses: Counter = Counter()
    spellings: dict[str, Counter] = {}
    for row in rows:
        raw = str(row["name"])
        canonical = synonyms.canonical_name(raw, _normal) or _normal(raw)
        if not canonical or canonical in known:
            continue
        uses[canonical] += int(row["uses"])
        spellings.setdefault(canonical, Counter())[raw] += int(row["uses"])

    return [
        (name, count, [word for word, _ in spellings[name].most_common(SPELLING_HINT_LIMIT)])
        for name, count in uses.most_common()
    ]


def write_batches(items: list[tuple[str, int, list[str]]]) -> int:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for index in range(0, len(items), BATCH_SIZE):
        chunk = items[index : index + BATCH_SIZE]
        number = index // BATCH_SIZE + 1
        path = os.path.join(OUTPUT_DIR, f"batch-{number:03d}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"# batch-{number:03d}\n")
            handle.write("имя | упоминаний | как пишется в рецептах\n")
            for name, count, spellings in chunk:
                variants = ", ".join(word for word in spellings if word != name)
                handle.write(f"{name} | {count} | {variants or '—'}\n")
    return -(-len(items) // BATCH_SIZE)


async def main() -> None:
    items = await collect()
    if not items:
        print("Справочник уже покрывает все ингредиенты готовых рецептов.")
        return
    batches = write_batches(items)
    once = sum(1 for _name, count, _ in items if count == 1)
    print(
        f"ингредиентов без КБЖУ: {len(items)} (из них {once} встречаются один раз); "
        f"пачек: {batches} по {BATCH_SIZE} в {OUTPUT_DIR}"
    )


if __name__ == "__main__":
    asyncio.run(main())
