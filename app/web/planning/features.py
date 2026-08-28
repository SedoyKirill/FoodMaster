"""Признаки блюда, которые видит оптимизатор (TZ-M8 §6.1).

`FeatureVector` из ТЗ — это расширение `CandidateScore`, а не второй класс
рядом: оценка кандидата и так считается один раз на план и уже несёт вкус,
ротацию, сезон и время. Здесь живёт вывод тех признаков, которые появились
в M8 и не сводятся к арифметике по ингредиентам: белковая база блюда и
макронутриенты кандидата.

Модуль ничего не знает ни о планировщике, ни о базе — только о рецепте и
словаре синонимов, поэтому его тесты не поднимают приложение.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

#: Порядок важен только для читаемости отчётов; «veg» — база по умолчанию:
#: блюдо без мяса, рыбы, яиц, молочного и бобовых считается овощным.
PROTEIN_BASES = ("meat", "poultry", "fish", "eggs", "dairy", "legumes", "veg")
DEFAULT_PROTEIN_BASE = "veg"

#: Базы, которые в ужинах ограничиваются жёстко (§6.2): именно они дают
#: ощущение «мы всю неделю едим одно и то же».
HARD_LIMITED_BASES = ("meat", "poultry", "fish")


def _ingredient_weight(ingredient: dict[str, Any]) -> Decimal:
    """Вес ингредиента в блюде — тот же порядок, что у ``main_ingredient``."""
    quantity = ingredient.get("quantity_max") or ingredient.get("quantity_min")
    try:
        value = Decimal(str(quantity)) if quantity is not None else Decimal("0")
    except ArithmeticError:
        value = Decimal("0")
    if ingredient.get("unit_code") in {"kg", "l"}:
        value *= 1000
    return value


def protein_base(recipe: dict[str, Any], synonyms: Any, normal: Any) -> str:
    """Белковая база блюда — по самому тяжёлому «белковому» ингредиенту.

    Не по главному ингредиенту: в плове главный — рис, но семья различает
    плов с курицей и плов со свининой, а не «плов и снова плов».
    """
    best_base = DEFAULT_PROTEIN_BASE
    best_weight = Decimal("-1")
    for ingredient in recipe.get("ingredients", []):
        name = str(
            ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
        )
        if not name:
            continue
        base = None
        for token in normal(name).split():
            base = synonyms.bases.get(synonyms.canonical_token(token)) or synonyms.bases.get(token)
            if base:
                break
        if not base:
            continue
        weight = _ingredient_weight(ingredient)
        if weight > best_weight:
            best_weight = weight
            best_base = base
    return best_base


def attach_bases(
    scores: dict[int, Any], recipes: list[dict[str, Any]], synonyms: Any, normal: Any
) -> None:
    """Проставляет белковую базу всем кандидатам разом."""
    for recipe in recipes:
        score = scores.get(int(recipe["id"]))
        if score is not None:
            score.protein_base = protein_base(recipe, synonyms, normal)
