"""Поиск «составных» рецептов: ингредиент — это другое блюдо той же книги.

Пример: «Салат с рулетом поркетта…» (id 29180) требует готовыми «Рулет
поркетта из свинины», «Соус песто из руколы» и «Вишнево-бальзамический
сироп» — все три описаны отдельными рецептами книги. Такой салат нельзя
предлагать в план как простое блюдо.

Запуск: DATABASE_URL=... python scripts/mark_compound_recipes.py [--apply]
Без --apply — только печать кандидатов (dry-run). С --apply ready-рецепты
переводятся в needs_review, в review_reasons добавляется "compound:<id>".
Идемпотентно: повторный прогон не дублирует причины.
"""

import asyncio
import json
import os
import re
import sys

import asyncpg

WORD_RE = re.compile(r"[а-яёa-z]{4,}", re.IGNORECASE)
#: ссылка дисквалифицирует, только если ссылаемое — самостоятельное блюдо.
#: Соусы, бульоны (dish_type NULL), заготовки и хлеб — нормальные
#: под-компоненты рецептов, их не считаем.
STANDALONE_REF_TYPES = {
    "main_course", "steak", "stew", "casserole", "cutlets", "soup", "salad",
    "pasta", "pizza", "dumplings", "pancakes", "porridge", "sandwich",
    "pie", "cake", "side",
}
#: слишком общие кулинарные слова — не считаются связующими
GENERIC_WORDS = {
    "салат", "блюдо", "рецепт", "домашний", "домашняя", "классический",
    "классическая", "быстрый", "быстрая", "вкусный", "вкусная", "свежий",
    "свежая", "зеленый", "зелёный", "красный", "белый", "чёрный", "черный",
}


def _stems(text: str) -> set[str]:
    """Стемы значимых слов: первые 5 букв — грубое снятие окончаний."""
    stems = set()
    for word in WORD_RE.findall(str(text).lower().replace("ё", "е")):
        if word in GENERIC_WORDS:
            continue
        stems.add(word[:5])
    return stems


def dish_reference(ingredient_name: str, titles: list[tuple[int, set[str]]]) -> int | None:
    """id рецепта книги, на который «ссылается» ингредиент, либо None.

    Ссылка — почти полное взаимное совпадение: пересечение стемов ≥2 слов
    И покрывает ≥2/3 стемов ингредиента И ≥2/3 стемов названия. Иначе
    «lemon zest» цепляется за любое название со словами lemon/zest.
    """
    ingredient_stems = _stems(ingredient_name)
    if len(ingredient_stems) < 2:
        return None
    for recipe_id, title_stems in titles:
        overlap = ingredient_stems & title_stems
        if (
            len(overlap) >= 2
            and len(overlap) * 3 >= len(ingredient_stems) * 2
            and len(overlap) * 3 >= len(title_stems) * 2
        ):
            return recipe_id
    return None


async def main() -> None:
    apply = "--apply" in sys.argv
    # --only 1,2,3 — применить только к подтверждённым id (после верификации
    # списка кандидатов Haiku-агентом).
    only: set[int] | None = None
    for index, arg in enumerate(sys.argv):
        if arg == "--only" and index + 1 < len(sys.argv):
            only = {int(part) for part in sys.argv[index + 1].split(",") if part.strip()}
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    recipes = await conn.fetch(
        """
        SELECT r.id, r.source_id, r.title, r.review_status, r.review_reasons,
               r.dish_type
        FROM recipe_library.recipes r
        WHERE r.review_status <> 'rejected'
        """
    )
    ingredients = await conn.fetch(
        """
        SELECT ri.recipe_id, ri.normalized_name
        FROM recipe_library.recipe_ingredients ri
        JOIN recipe_library.recipes r ON r.id = ri.recipe_id
        WHERE r.review_status = 'ready' AND ri.normalized_name IS NOT NULL
        """
    )

    titles_by_source: dict[int, list[tuple[int, set[str]]]] = {}
    title_by_id: dict[int, str] = {}
    source_by_id: dict[int, int] = {}
    for row in recipes:
        stems = _stems(row["title"])
        title_by_id[row["id"]] = row["title"]
        source_by_id[row["id"]] = row["source_id"]
        if len(stems) >= 2 and row["dish_type"] in STANDALONE_REF_TYPES:
            titles_by_source.setdefault(row["source_id"], []).append((row["id"], stems))

    ings_by_recipe: dict[int, list[str]] = {}
    for row in ingredients:
        ings_by_recipe.setdefault(row["recipe_id"], []).append(row["normalized_name"])

    found: dict[int, list[tuple[str, int]]] = {}
    for recipe_id, names in ings_by_recipe.items():
        candidates = [
            (other_id, stems)
            for other_id, stems in titles_by_source.get(source_by_id.get(recipe_id, -1), [])
            if other_id != recipe_id
        ]
        for name in names:
            ref = dish_reference(name, candidates)
            if ref is not None:
                found.setdefault(recipe_id, []).append((name, ref))

    print(f"кандидатов-составных: {len(found)}")
    for recipe_id, refs in sorted(found.items()):
        print(f"  [{recipe_id}] {title_by_id[recipe_id]}")
        for name, ref in refs:
            print(f"      «{name}» → [{ref}] {title_by_id[ref]}")

    if only is not None:
        found = {recipe_id: refs for recipe_id, refs in found.items() if recipe_id in only}
        print(f"после фильтра --only: {len(found)}")

    if apply and found:
        updated = 0
        async with conn.transaction():
            for recipe_id, refs in found.items():
                row = await conn.fetchrow(
                    "SELECT review_reasons FROM recipe_library.recipes WHERE id=$1",
                    recipe_id,
                )
                reasons = row["review_reasons"]
                reasons = json.loads(reasons) if isinstance(reasons, str) else list(reasons or [])
                new_reasons = [f"compound:{ref}" for _, ref in refs if f"compound:{ref}" not in reasons]
                if not new_reasons and not apply:
                    continue
                result = await conn.execute(
                    """
                    UPDATE recipe_library.recipes
                    SET review_status='needs_review', review_reasons=$2::jsonb
                    WHERE id=$1
                    """,
                    recipe_id, json.dumps(reasons + new_reasons, ensure_ascii=False),
                )
                updated += result.endswith(" 1")
        print(f"переведено в needs_review: {updated}")
    elif not apply:
        print("dry-run: изменений нет (добавьте --apply)")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
