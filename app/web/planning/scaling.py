"""Порции и масштабирование ингредиентов (TZ-M5R §2.4).

Никакой молчаливой подмены неизвестных порций «на 4» (дефект P5): рецепт без
servings не масштабируется, помечается ``scale_unknown`` и считается на один
базовый рецепт. Количества — Decimal, округление только при отображении.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any


def desired_servings(people: list[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for person in people:
        factor = person.get("portion_factor")
        if factor is None:
            factor = "0.65" if person.get("person_type") == "child" else "1"
        total += Decimal(str(factor))
    return total or Decimal("1")


def recipe_scale(
    recipe: dict[str, Any], required_servings: Decimal
) -> tuple[Decimal | None, bool]:
    """(scale, scale_unknown): None+True, когда в рецепте нет порций/выхода."""
    source = recipe.get("source_servings_min")
    if source is None:
        return None, True
    source_decimal = Decimal(str(source))
    if source_decimal <= 0:
        return None, True
    return required_servings / source_decimal, False


def scaled_quantity(
    ingredient: dict[str, Any], scale: Decimal | None
) -> Decimal | None:
    """Количество ингредиента с учётом масштаба.

    ``is_to_taste`` не масштабируется (соль «по вкусу» не становится «по вкусу
    ×1.5»); при неизвестном масштабе количества остаются «как в книге».
    """
    quantity = ingredient.get("quantity_max") or ingredient.get("quantity_min")
    if quantity is None:
        return None
    value = Decimal(str(quantity))
    if ingredient.get("is_to_taste") or scale is None:
        return value
    return value * scale


def display_quantity(quantity: Decimal | None, unit: str | None) -> Decimal | None:
    """Округление только на выводе: г/мл — до 5, штучные — вверх до целого."""
    if quantity is None:
        return None
    if unit in {"g", "ml"}:
        step = Decimal("5")
        return (quantity / step).to_integral_value(rounding=ROUND_HALF_UP) * step
    if unit == "piece":
        return quantity.to_integral_value(rounding=ROUND_CEILING)
    return quantity
