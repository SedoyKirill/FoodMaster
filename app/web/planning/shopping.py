"""Список покупок и запасы (TZ-M5R §2.5).

Агрегация по каноническому имени из словаря синонимов («молоко» и «молока» —
одна позиция; дефект P4), совместимые единицы складываются в базовых (г, мл,
шт), запасы списываются FEFO, упаковки Ленты — с целым числом пачек и
излишком. Позиции без товарного сопоставления помечаются ``unmatched`` —
UI собирает их в секцию «уточнить в магазине».
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Any

from .candidates import Synonyms, canonical_overlap

#: то, что есть из-под крана и не покупается — в список покупок не попадает
PANTRY_FREE = {"вода", "кипяток", "лед", "лёд"}


def aggregate_ingredients(
    meal_ingredients: list[dict[str, Any]],
    synonyms: Synonyms,
    normal: Any,
    base_quantity: Any,
) -> dict[tuple[str, str | None], dict[str, Any]]:
    """Суммирует потребности по (canonical, базовая единица).

    ``is_to_taste``-ингредиенты в покупки не попадают (TZ §2.4) — кроме
    случая, когда их нет в запасах: тогда это одна позиция без количества;
    это решает вызывающая сторона по флагу ``to_taste``.
    """
    aggregate: dict[tuple[str, str | None], dict[str, Any]] = {}
    for item in meal_ingredients:
        name = str(item.get("name") or "")
        canonical = synonyms.canonical_name(name, normal) or normal(name)
        quantity = item.get("quantity")
        unit = item.get("unit_code")
        quantity_base, unit_base = base_quantity(quantity, unit)
        key = (canonical, unit_base)
        entry = aggregate.setdefault(
            key,
            {
                "normalized_name": canonical,
                "unit_code": unit_base,
                "quantity": None,
                "to_taste": False,
                "display_name": name,
            },
        )
        if item.get("is_to_taste"):
            entry["to_taste"] = True
            continue
        if quantity_base is not None:
            entry["quantity"] = (entry["quantity"] or Decimal("0")) + quantity_base
    return aggregate


def prepare_inventory(
    inventory: list[dict[str, Any]],
    synonyms: Synonyms,
    normal: Any,
    base_quantity: Any,
) -> list[dict[str, Any]]:
    prepared = []
    for lot in inventory:
        quantity, unit = base_quantity(Decimal(str(lot["quantity"])), lot.get("unit_code"))
        prepared.append(
            {
                **lot,
                "base_quantity": quantity or Decimal("0"),
                "base_unit": unit,
                "canonical": synonyms.canonical_name(str(lot.get("name") or ""), normal),
            }
        )
    return prepared


def _has_stock(lots: list[dict[str, Any]], canonical: str) -> bool:
    """Есть ли продукт дома хоть в каком-то количестве (единица не важна —
    «соль по вкусу» покрывается солью в любой фасовке)."""
    return any(
        canonical_overlap(lot["canonical"], canonical) and lot["base_quantity"] > 0
        for lot in lots
    )


def _consume_fefo(
    lots: list[dict[str, Any]], canonical: str, unit: str | None, needed: Decimal
) -> Decimal:
    """Списывает из запасов по FEFO; возвращает покрытое количество."""
    covered = Decimal("0")
    matching = sorted(
        (
            lot
            for lot in lots
            if lot["base_unit"] == unit and canonical_overlap(lot["canonical"], canonical)
        ),
        key=lambda lot: (lot.get("expires_on") is None, lot.get("expires_on") or date.max),
    )
    for lot in matching:
        if needed <= 0:
            break
        take = min(needed, lot["base_quantity"])
        if take <= 0:
            continue
        lot["base_quantity"] -= take
        covered += take
        needed -= take
    return covered


def build_shopping(
    aggregate: dict[tuple[str, str | None], dict[str, Any]],
    inventory_lots: list[dict[str, Any]],
    matcher: Any,
    price_tier: str,
    base_quantity: Any,
) -> tuple[list[dict[str, Any]], int, int]:
    """Список покупок: FEFO-покрытие, упаковки, стоимость.

    Возвращает (позиции, суммарная стоимость, число позиций с ценой).
    """
    shopping: list[dict[str, Any]] = []
    total_cost = 0
    matched_cost_items = 0
    for (canonical, unit), entry in sorted(
        aggregate.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        first_word = canonical.split()[0] if canonical else ""
        if canonical in PANTRY_FREE or first_word in PANTRY_FREE:
            continue
        quantity = entry["quantity"]
        to_taste_only = entry["to_taste"] and quantity is None
        covered = Decimal("0")
        if quantity is not None:
            covered = _consume_fefo(inventory_lots, canonical, unit, quantity)
            buy_quantity: Decimal | None = max(Decimal("0"), quantity - covered)
        elif to_taste_only:
            # «По вкусу»: в покупки только если дома совсем нет (TZ §2.4).
            if _has_stock(inventory_lots, canonical):
                continue
            buy_quantity = None
        else:
            buy_quantity = None

        matched_product = (
            matcher.match(canonical, unit, price_tier, buy_quantity)
            if buy_quantity is not None and buy_quantity > 0
            else None
        )
        pack_count = None
        estimated_cost = None
        leftover = None
        if matched_product and buy_quantity is not None and buy_quantity > 0:
            pack_base, pack_base_unit = base_quantity(
                Decimal(str(matched_product.get("pack_quantity") or 0)),
                matched_product.get("pack_unit"),
            )
            if pack_base and pack_base_unit == unit and pack_base > 0:
                pack_count = math.ceil(buy_quantity / pack_base)
                estimated_cost = pack_count * int(matched_product["effective_price_kop"])
                leftover = pack_count * pack_base - buy_quantity
                total_cost += estimated_cost
                matched_cost_items += 1
        shopping.append(
            {
                "normalized_name": canonical,
                "quantity": quantity,
                "unit_code": unit,
                "covered_from_inventory": covered,
                "buy_quantity": buy_quantity,
                "matched_product_id": matched_product.get("id") if matched_product else None,
                "matched_product_name": matched_product.get("name") if matched_product else None,
                "pack_count": pack_count,
                "estimated_cost_kop": estimated_cost,
                "leftover_quantity": leftover,
                "to_taste": entry["to_taste"],
                "unmatched": bool(
                    buy_quantity is not None and buy_quantity > 0 and not matched_product
                ),
            }
        )
    return shopping, total_cost, matched_cost_items
