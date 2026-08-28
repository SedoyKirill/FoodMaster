"""Кандидаты и оценка блюд (TZ-M5R §2.1–2.2).

Синонимы приходят из ``app_core.ingredient_synonyms`` разделёнными на два
словаря: ``forms`` (словоформа → канонический продукт, используется и в
агрегации покупок, и в ограничениях) и ``groups`` (канонический продукт →
аллергенная группа, только ограничения). Никаких substring-эвристик:
правило матчит ингредиент только через словарь или точное совпадение токена.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ..nutrition import ingredient_kcal
from ..planner import json_list

HARD_RULE_TYPES = {"allergy", "intolerance", "exclude"}
EXPIRY_SOON_DAYS = 4
#: сколько ready-рецептов считается достаточным, прежде чем добирать черновики
MIN_READY_CANDIDATES = 60


class Synonyms:
    """Словарь синонимов с двумя доменами: словоформы и аллергенные группы."""

    def __init__(
        self,
        forms: dict[str, str] | None = None,
        groups: dict[str, str] | None = None,
    ) -> None:
        self.forms = forms or {}
        self.groups = groups or {}

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> "Synonyms":
        forms: dict[str, str] = {}
        groups: dict[str, str] = {}
        for row in rows:
            term = str(row.get("term") or "").strip().casefold()
            canonical = str(row.get("canonical") or "").strip().casefold()
            if not term or not canonical:
                continue
            if row.get("kind") == "group":
                groups[term] = canonical
            else:
                forms[term] = canonical
        return cls(forms, groups)

    def canonical_token(self, token: str) -> str:
        return self.forms.get(token, token)

    def canonical_name(self, name: str, normal: Any) -> str:
        """Каноническое имя для агрегации: словоформы приводятся к продукту."""
        seen: list[str] = []
        for token in normal(str(name)).split():
            canonical = self.canonical_token(token)
            if canonical not in seen:
                seen.append(canonical)
        return " ".join(seen)


def hard_rule_terms(rules: list[dict[str, Any]], synonyms: Synonyms, normal: Any) -> set[str]:
    """Канонические термы жёстких правил (allergy/intolerance/exclude).

    Правило без rule_type (исторические данные) считается жёстким, если
    ``is_hard`` истинно; ``dislike`` — никогда не жёсткое (TZ-M5R §2.1).
    """
    result: set[str] = set()
    for rule in rules:
        rule_type = rule.get("rule_type")
        if rule_type == "dislike":
            continue
        if not rule.get("is_hard", True):
            continue
        if rule_type is not None and rule_type not in HARD_RULE_TYPES:
            continue
        for token in normal(str(rule.get("term") or "")).split():
            result.add(synonyms.canonical_token(token))
    result.discard("")
    return result


def soft_rule_terms(rules: list[dict[str, Any]], synonyms: Synonyms, normal: Any) -> set[str]:
    """Термы нежёстких правил: dislike и любые is_hard=false."""
    result: set[str] = set()
    for rule in rules:
        if rule.get("rule_type") == "dislike" or not rule.get("is_hard", True):
            for token in normal(str(rule.get("term") or "")).split():
                result.add(synonyms.canonical_token(token))
    result.discard("")
    return result


def ingredient_matches_terms(
    name: str, terms: set[str], synonyms: Synonyms, normal: Any
) -> bool:
    if not terms:
        return False
    for token in normal(str(name)).split():
        canonical = synonyms.canonical_token(token)
        if canonical in terms:
            return True
        group = synonyms.groups.get(canonical)
        if group is not None and group in terms:
            return True
    return False


def recipe_matches_terms(
    recipe: dict[str, Any], terms: set[str], synonyms: Synonyms, normal: Any
) -> int:
    """Число ингредиентов рецепта, совпавших с термами (для dislike-штрафа)."""
    count = 0
    for ingredient in recipe.get("ingredients", []):
        name = str(
            ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
        )
        if ingredient_matches_terms(name, terms, synonyms, normal):
            count += 1
    return count


def main_ingredient(recipe: dict[str, Any], synonyms: Synonyms, normal: Any) -> str | None:
    """Главный ингредиент — самый «тяжёлый» по базовому количеству (для
    разнообразия в соседние дни)."""
    best_name: str | None = None
    best_quantity = Decimal("-1")
    for position, ingredient in enumerate(recipe.get("ingredients", [])):
        name = str(
            ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
        )
        if not name:
            continue
        quantity = ingredient.get("quantity_max") or ingredient.get("quantity_min")
        try:
            value = Decimal(str(quantity)) if quantity is not None else Decimal("0")
        except ArithmeticError:
            value = Decimal("0")
        unit = ingredient.get("unit_code")
        if unit in {"kg", "l"}:
            value *= 1000
        if value > best_quantity or (best_name is None and position == 0):
            best_quantity = value
            best_name = synonyms.canonical_name(name, normal)
    return best_name


class CandidateScore:
    """Оценка блюда для целевой функции (TZ-M5R §2.2)."""

    __slots__ = (
        "recipe_id", "cost_kop", "cost_estimated", "kcal", "kcal_estimated",
        "expiry_bonus", "dislike_penalty", "cuisine_bonus", "meal_fit",
        "meal_bias", "main_ingredient", "draft", "rating_bonus", "dish_type",
    )

    def __init__(self, recipe_id: int) -> None:
        self.recipe_id = recipe_id
        self.cost_kop = 0
        self.cost_estimated = False
        self.kcal: int | None = None
        self.kcal_estimated = True
        self.expiry_bonus = 0
        self.dislike_penalty = 0
        self.cuisine_bonus = 0
        self.meal_fit: dict[str, float] = {}
        self.meal_bias: dict[str, float] = {}
        self.main_ingredient: str | None = None
        self.draft = False
        self.rating_bonus = 0
        self.dish_type: str | None = None


def _expiring_canonicals(
    inventory: list[dict[str, Any]], starts_on: date, synonyms: Synonyms, normal: Any
) -> set[str]:
    horizon = starts_on + timedelta(days=EXPIRY_SOON_DAYS)
    result: set[str] = set()
    for lot in inventory:
        expires_on = lot.get("expires_on")
        if expires_on is None or expires_on > horizon:
            continue
        result.add(synonyms.canonical_name(str(lot.get("name") or ""), normal))
    result.discard("")
    return result


def score_candidates(
    candidates: list[dict[str, Any]],
    *,
    meal_types: list[str],
    cuisines: list[str],
    rules: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    starts_on: date,
    synonyms: Synonyms,
    normal: Any,
    tokens: Any,
    cost_hint: Any,
    meal_score: Any,
    kcal_hint: Any = None,
) -> dict[int, CandidateScore]:
    """Считает оценку каждого кандидата один раз (TZ-M5R §2.2).

    ``normal``/``tokens``/``cost_hint``/``meal_score`` инъектируются из
    ``planner.py``, чтобы не создавать циклический импорт.
    """
    soft_terms = soft_rule_terms(rules, synonyms, normal)
    expiring = _expiring_canonicals(inventory, starts_on, synonyms, normal)
    cuisine_set = set(cuisines)
    inventory_canonicals = {
        synonyms.canonical_name(str(lot.get("name") or ""), normal)
        for lot in inventory
    }
    inventory_canonicals.discard("")

    scores: dict[int, CandidateScore] = {}
    raw_costs: dict[int, tuple[int, int]] = {}  # id -> (стоимость, непокрытых)
    all_item_costs: list[int] = []  # цены отдельных сопоставленных ингредиентов
    for recipe in candidates:
        recipe_id = int(recipe["id"])
        score = CandidateScore(recipe_id)
        score.draft = recipe.get("review_status") != "ready"
        score.cuisine_bonus = 1 if cuisine_set and recipe.get("cuisine_code") in cuisine_set else 0
        score.dislike_penalty = recipe_matches_terms(recipe, soft_terms, synonyms, normal)
        score.main_ingredient = main_ingredient(recipe, synonyms, normal)
        # Тип блюда нужен оптимизатору: без него три завтрака подряд —
        # блины, потому что они дешевле любой каши и омлета.
        dish_type = recipe.get("dish_type")
        score.dish_type = str(dish_type) if dish_type else None

        # meal_types из JSONB может прийти строкой '["drink"]' — json_list
        # разбирает оба представления (без него fit ломался у всей библиотеки).
        recipe_meal_types = set(json_list(recipe.get("meal_types")))
        for meal_type in meal_types:
            # meal_fit — строго по TZ §2.2: тег совпал / тегов нет / не совпал.
            # Эвристика заголовков («каша» → завтрак) идёт отдельным малым
            # смещением: она не должна превращать напиток в обед.
            if recipe.get("dish_type") == "drink":
                # Напиток — не самостоятельный приём пищи: допускается в слот
                # только при тотальной нехватке кандидатов (иерархия слота).
                fit = 0.0
            elif meal_type in recipe_meal_types:
                fit = 1.0
            elif not recipe_meal_types:
                fit = 0.5
            else:
                fit = 0.0
            score.meal_fit[meal_type] = fit
            heuristic = meal_score(recipe, meal_type)
            score.meal_bias[meal_type] = max(-0.5, min(0.5, heuristic / 40))

        kcal_total = Decimal("0")
        kcal_known = False
        matched_costs: list[int] = []
        unmatched = 0
        for ingredient in recipe.get("ingredients", []):
            name = str(
                ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""
            )
            canonical = synonyms.canonical_name(name, normal)
            if canonical and canonical in expiring:
                score.expiry_bonus += 1
            quantity = ingredient.get("quantity_max") or ingredient.get("quantity_min")
            quantity_decimal = Decimal(str(quantity)) if quantity is not None else None
            # K2/N2: словоформы («масла», «муки») приводятся к канону, иначе
            # substring-справочник ккал молча промахивается. kcal_hint —
            # инъекция из planner: сначала таблица ingredient_nutrition.
            if kcal_hint is not None:
                kcal = kcal_hint(name, canonical, quantity_decimal, ingredient.get("unit_code"))
            else:
                kcal = ingredient_kcal(canonical or name, quantity_decimal, ingredient.get("unit_code"))
            if kcal is not None:
                kcal_total += kcal
                kcal_known = True
            if canonical and canonical in inventory_canonicals:
                continue  # дома есть — для оценки бесплатно (TZ §2.2)
            item_cost = cost_hint(ingredient)
            if item_cost is None:
                unmatched += 1
            else:
                matched_costs.append(item_cost)
        if kcal_known:
            score.kcal = int(kcal_total)
        raw_costs[recipe_id] = (sum(matched_costs), unmatched)
        all_item_costs.extend(matched_costs)
        if unmatched:
            score.cost_estimated = True
        scores[recipe_id] = score

    # K1: медиана считается по ценам отдельных сопоставленных ингредиентов.
    # Раньше сумма рецепта делилась на размер пула кандидатов (до 500), из-за
    # чего несопоставленные ингредиенты оценивались почти в ноль и солвер
    # систематически предпочитал рецепты с непрайсуемыми продуктами.
    positive_costs = sorted(cost for cost in all_item_costs if cost > 0)
    median_cost = positive_costs[len(positive_costs) // 2] if positive_costs else 0
    for recipe_id, (cost, unmatched) in raw_costs.items():
        scores[recipe_id].cost_kop = cost + unmatched * median_cost
    return scores
