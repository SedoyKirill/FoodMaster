"""Проверка JSON-результатов сессии Haiku и правила готовности (TZ-M2R, этап 5)."""

from __future__ import annotations

import re
from typing import Any

ALLOWED_MEAL_TYPES = {"breakfast", "lunch", "dinner", "dessert", "snack", "drink"}
ALLOWED_DIET_TAGS = {"vegan_claim", "vegetarian_claim", "sugar_free_claim", "gluten_free_claim"}
ALLOWED_APPLIANCES = {
    "oven", "stove", "microwave", "pressure_cooker", "multicooker", "air_fryer",
    "electric_grill", "grill", "blender", "mixer", "food_processor", "autoclave",
    "steamer", "fridge_freezer",
}
ALLOWED_UNITS = {
    "g", "kg", "mg", "ml", "l", "piece", "tablespoon", "teaspoon", "cup", "glass",
    "clove", "bunch", "can", "package", "pinch", "slice", "stalk", "leaf", "sprig",
    "handful", "drop",
}

RECIPE_WORD_RE = re.compile(
    r"(ингредиент|способ приготовления|приготовление:|ingredients|порци|servings)",
    re.IGNORECASE,
)
QUANTITY_LINE_RE = re.compile(
    r"^\s*\d+(?:[.,/]\d+)?\s*(г|кг|мл|л|шт|ст\.?\s*л|ч\.?\s*л|стакан|cup|tablespoon|teaspoon|tbsp|tsp|pound|ounce)\b",
    re.IGNORECASE | re.MULTILINE,
)


def looks_like_recipe_window(body: str) -> bool:
    """Грубая проверка: похоже ли окно на страницу с рецептом.

    Используется как защита от «заглушек»: пустой результат для такого окна
    отвергается — окно возвращается в очередь необработанных. Требуются и
    словесные маркеры, и строки с количествами — иначе проза о еде даёт
    ложные срабатывания.
    """
    quantity_lines = len(QUANTITY_LINE_RE.findall(body))
    words = len(RECIPE_WORD_RE.findall(body))
    return quantity_lines >= 2 and (words >= 1 or quantity_lines >= 5)


COOKING_VERB_RE = re.compile(
    r"(нареж|нарез|смеша|перемеша|взбе|взбив|обжар|вылож|запека|запеч|добав|раздав|измельч|"
    r"разогре|выпека|влить|залить|варить|туши)",
    re.IGNORECASE,
)

_NUMBER = (int, float)


def _err(errors: list[str], path: str, message: str) -> None:
    errors.append(f"{path}: {message}")


def _check_enum_list(value: Any, allowed: set[str], path: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        _err(errors, path, "ожидается массив")
        return
    for item in value:
        if not isinstance(item, str) or item not in allowed:
            _err(errors, path, f"недопустимое значение {item!r}")


def _check_optional_number(value: Any, path: str, errors: list[str]) -> None:
    if value is not None and not isinstance(value, _NUMBER):
        _err(errors, path, "ожидается число или null")


def _check_ingredient(item: Any, path: str, errors: list[str]) -> None:
    if not isinstance(item, dict):
        _err(errors, path, "ожидается объект")
        return
    for field in ("raw_text", "name"):
        if not isinstance(item.get(field), str) or not item.get(field, "").strip():
            _err(errors, f"{path}.{field}", "ожидается непустая строка")
    if not isinstance(item.get("is_to_taste"), bool):
        _err(errors, f"{path}.is_to_taste", "ожидается true/false")
    _check_optional_number(item.get("quantity_min"), f"{path}.quantity_min", errors)
    _check_optional_number(item.get("quantity_max"), f"{path}.quantity_max", errors)
    unit = item.get("unit")
    if unit is not None and unit not in ALLOWED_UNITS:
        _err(errors, f"{path}.unit", f"недопустимая единица {unit!r}")
    for field in ("section", "note"):
        value = item.get(field)
        if value is not None and not isinstance(value, str):
            _err(errors, f"{path}.{field}", "ожидается строка или null")


def _check_recipe(recipe: Any, path: str, errors: list[str]) -> None:
    if not isinstance(recipe, dict):
        _err(errors, path, "ожидается объект")
        return
    if not isinstance(recipe.get("title"), str) or not recipe.get("title", "").strip():
        _err(errors, f"{path}.title", "ожидается непустая строка")
    if not isinstance(recipe.get("page_start"), int):
        _err(errors, f"{path}.page_start", "ожидается целое число")
    if recipe.get("page_end") is not None and not isinstance(recipe.get("page_end"), int):
        _err(errors, f"{path}.page_end", "ожидается целое число или null")
    if not isinstance(recipe.get("is_complete"), bool):
        _err(errors, f"{path}.is_complete", "ожидается true/false")
    _check_optional_number(recipe.get("servings_min"), f"{path}.servings_min", errors)
    _check_optional_number(recipe.get("servings_max"), f"{path}.servings_max", errors)
    if recipe.get("yield_text") is not None and not isinstance(recipe.get("yield_text"), str):
        _err(errors, f"{path}.yield_text", "ожидается строка или null")
    if recipe.get("time_total_minutes") is not None and not isinstance(
        recipe.get("time_total_minutes"), int
    ):
        _err(errors, f"{path}.time_total_minutes", "ожидается целое число или null")
    _check_enum_list(recipe.get("meal_types", []), ALLOWED_MEAL_TYPES, f"{path}.meal_types", errors)
    _check_enum_list(recipe.get("diet_tags", []), ALLOWED_DIET_TAGS, f"{path}.diet_tags", errors)
    _check_enum_list(recipe.get("appliances", []), ALLOWED_APPLIANCES, f"{path}.appliances", errors)
    ingredients = recipe.get("ingredients")
    if not isinstance(ingredients, list):
        _err(errors, f"{path}.ingredients", "ожидается массив")
    else:
        for index, item in enumerate(ingredients):
            _check_ingredient(item, f"{path}.ingredients[{index}]", errors)
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
        _err(errors, f"{path}.steps", "ожидается массив строк")


def validate_payload(payload: Any) -> list[str]:
    """Проверяет файл результата целиком; возвращает список ошибок (пустой = ок)."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["корень: ожидается объект"]
    for field in ("window_id", "sha256", "model"):
        if not isinstance(payload.get(field), str) or not payload.get(field, "").strip():
            _err(errors, field, "ожидается непустая строка")
    if not isinstance(payload.get("schema_version"), int):
        _err(errors, "schema_version", "ожидается целое число")
    recipes = payload.get("recipes")
    if not isinstance(recipes, list):
        _err(errors, "recipes", "ожидается массив")
        return errors
    for index, recipe in enumerate(recipes):
        _check_recipe(recipe, f"recipes[{index}]", errors)
    return errors


def review_recipe(recipe: dict, source_title: str | None) -> list[str]:
    """Правила готовности (TZ-M2R §2, этап 5.5): пустой список причин = ready."""
    reasons: list[str] = []
    ingredients = recipe.get("ingredients") or []
    steps = recipe.get("steps") or []
    if len(ingredients) < 2:
        reasons.append("too_few_ingredients")
    if not steps:
        reasons.append("no_steps")

    quantitative = 0
    with_unit = 0
    for item in ingredients:
        name = (item.get("name") or "").strip()
        if not name or len(name) > 60:
            reasons.append("ingredient_name_bad")
        if COOKING_VERB_RE.search(name):
            reasons.append("ingredient_looks_like_step")
        if item.get("quantity_min") is None and not item.get("is_to_taste"):
            reasons.append("ingredient_without_quantity")
        if item.get("quantity_min") is not None:
            quantitative += 1
            if item.get("unit"):
                with_unit += 1
    if quantitative and with_unit / quantitative < 0.8:
        reasons.append("units_missing")

    title = (recipe.get("title") or "").strip()
    letters = sum(char.isalpha() for char in title)
    if not 3 <= len(title) <= 120 or (title and letters / max(1, len(title)) < 0.6):
        reasons.append("title_bad")
    elif title[:1].isdigit():
        reasons.append("title_bad")
    elif title == title.upper() and len(title) > 25:
        reasons.append("title_bad")
    elif source_title and title.lower() == source_title.lower():
        reasons.append("title_is_book_title")

    if recipe.get("servings_min") is None and not recipe.get("yield_text"):
        reasons.append("no_yield")

    if steps and (any(len(step) < 15 for step in steps) or sum(len(step) for step in steps) < 80):
        reasons.append("steps_too_short")

    return sorted(set(reasons))
