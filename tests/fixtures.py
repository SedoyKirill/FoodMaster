"""Общие фикстуры тестов (TZ-TESTS §2.4, §4).

Обычные словари с перекрытием через ``**overrides`` — никаких JSON-файлов.
Формы данных повторяют то, что реально отдают запросы из ``app/web/database.py``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any


def make_ingredient(**overrides: Any) -> dict[str, Any]:
    """Строка ``recipe_library.recipe_ingredients`` в том виде, в каком её читает web."""
    base: dict[str, Any] = {
        "position": 1,
        "raw_text": "Молоко - 200 мл",
        "quantity_min": Decimal("200"),
        "quantity_max": Decimal("200"),
        "unit_raw": "ml",
        "unit_code": "ml",
        "ingredient_text": "молоко",
        "normalized_name": "молоко",
        "parsing_confidence": Decimal("0.9"),
        "is_to_taste": False,
        "section": None,
        "note": None,
    }
    base.update(overrides)
    return base


def make_recipe(**overrides: Any) -> dict[str, Any]:
    """Рецепт в форме, которую ждёт ``build_plan`` и отдаёт ``planner_data``."""
    recipe_id = overrides.pop("id", 1)
    ingredient = overrides.pop("ingredient", None)
    base: dict[str, Any] = {
        "id": recipe_id,
        "title": f"Блюдо {recipe_id}",
        "source_page_start": recipe_id,
        "source_servings_min": Decimal("2"),
        "cuisine_code": None,
        "dish_type": None,
        "meal_types": ["lunch", "dinner"],
        "appliances": [],
        "review_status": "ready",
        "extraction_confidence": Decimal("0.9"),
        "ingredient_count": 3,
        "step_count": 2,
        "ingredients": [
            make_ingredient(position=1, ingredient_text="молоко", normalized_name="молоко"),
            make_ingredient(
                position=2, ingredient_text="мука", normalized_name="мука",
                unit_raw="g", unit_code="g", quantity_min=Decimal("150"),
                quantity_max=Decimal("150"), raw_text="Мука - 150 г",
            ),
            make_ingredient(
                position=3, ingredient_text="яйцо", normalized_name="яйцо",
                unit_raw="piece", unit_code="piece", quantity_min=Decimal("2"),
                quantity_max=Decimal("2"), raw_text="Яйцо - 2 шт",
            ),
        ],
    }
    if ingredient is not None:
        base["ingredients"] = [
            make_ingredient(ingredient_text=ingredient, normalized_name=ingredient.casefold())
        ]
        base["ingredient_count"] = 1
    base.update(overrides)
    return base


def make_recipe_row(**overrides: Any) -> dict[str, Any]:
    """Строка ответа ``list_recipes`` (то, что уходит в интерфейс)."""
    recipe_id = overrides.pop("id", 1)
    base: dict[str, Any] = {
        "id": recipe_id,
        "title": f"Блюдо {recipe_id}",
        "source_page_start": 12,
        "source_page_end": 13,
        "source_servings_min": None,
        "source_servings_max": None,
        "source_yield_text": None,
        "cuisine_code": None,
        "meal_types": "[]",
        "diet_tags": "[]",
        "appliances": "[]",
        "review_status": "ready",
        "ingredient_count": 3,
        "step_count": 2,
        "time_total_minutes": None,
        "extraction_confidence": Decimal("0.9"),
        "ingredient_names": ["молоко", "мука", "яйцо"],
        "total_count": 1,
    }
    base.update(overrides)
    return base


def make_person(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": "Взрослый",
        "person_type": "adult",
        "target_kcal": None,
        "portion_factor": Decimal("1"),
    }
    base.update(overrides)
    return base


def make_product(**overrides: Any) -> dict[str, Any]:
    product_id = overrides.pop("id", 1)
    base: dict[str, Any] = {
        "id": product_id,
        "name": "Молоко «Простоквашино» 3,2%",
        "brand": "Простоквашино",
        "pack_text": "930 мл",
        "pack_quantity": Decimal("930"),
        "pack_unit": "ml",
        "regular_price_kop": 11900,
        "loyalty_price_kop": 9900,
        "promo_price_kop": None,
        "effective_price_kop": 9900,
        "category_slugs": ["moloko-syr-yaytsa"],
    }
    base.update(overrides)
    return base


def make_inventory_lot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": "Молоко",
        "quantity": Decimal("1000"),
        "unit_code": "ml",
        "expires_on": None,
        "storage_area": "fridge",
        "created_at": "2026-08-17T09:00:00+03:00",
    }
    base.update(overrides)
    return base


def make_plan(**overrides: Any) -> dict[str, Any]:
    """Сохранённый план в форме ответа ``latest_plan`` / ``get_plan``."""
    plan_id = overrides.pop("id", None) or str(uuid.uuid4())
    base: dict[str, Any] = {
        "id": plan_id,
        "starts_on": date(2026, 8, 17).isoformat(),
        "days": 3,
        "budget_kop": None,
        "estimated_cost_kop": 124000,
        "matched_cost_items": 21,
        "total_cost_items": 32,
        "cuisine_preferences": "[]",
        "price_tier": "balanced",
        "mode": "balanced",
        "created_at": "2026-08-17T09:00:00+03:00",
        "status": "draft",
        "meals": [],
        "shopping": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def make_shopping_item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "normalized_name": "молоко",
        "quantity": Decimal("1000"),
        "unit_code": "ml",
        "covered_from_inventory": Decimal("0"),
        "buy_quantity": Decimal("1000"),
        "matched_product_id": 1,
        "matched_product_name": "Молоко «Простоквашино» 3,2%",
        "pack_count": 2,
        "estimated_cost_kop": 19800,
        "purchased_at": None,
        "category_slug": "moloko-syr-yaytsa",
    }
    base.update(overrides)
    return base
