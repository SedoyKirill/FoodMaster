"""Кандидаты и оценка блюд (TZ-M5R §2.1–2.2).

Синонимы приходят из ``app_core.ingredient_synonyms`` разделёнными на три
словаря: ``forms`` (словоформа → канонический продукт, используется и в
агрегации покупок, и в ограничениях), ``groups`` (канонический продукт →
аллергенная группа, только ограничения) и ``bases`` (продукт → белковая база
блюда, TZ-M8 §6.1). Никаких substring-эвристик: правило матчит ингредиент
только через словарь или точное совпадение токена.
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
    """Словарь синонимов: словоформы, аллергенные группы и белковые базы."""

    def __init__(
        self,
        forms: dict[str, str] | None = None,
        groups: dict[str, str] | None = None,
        bases: dict[str, str] | None = None,
    ) -> None:
        self.forms = forms or {}
        self.groups = groups or {}
        #: продукт → белковая база блюда (TZ-M8 §6.1)
        self.bases = bases or {}

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> "Synonyms":
        forms: dict[str, str] = {}
        groups: dict[str, str] = {}
        bases: dict[str, str] = {}
        by_kind = {"group": groups, "protein_base": bases}
        for row in rows:
            term = str(row.get("term") or "").strip().casefold()
            canonical = str(row.get("canonical") or "").strip().casefold()
            if not term or not canonical:
                continue
            by_kind.get(str(row.get("kind")), forms)[term] = canonical
        return cls(forms, groups, bases)

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


def canonical_overlap(lot_canonical: str, canonical: str) -> bool:
    """Считается ли запас тем же продуктом, что ингредиент рецепта.

    Живёт здесь, а не в ``shopping``: одно и то же сравнение нужно и списку
    покупок, и оценке кандидата (иначе «сметана 20%» дома покрывает покупку,
    но не удешевляет блюдо в глазах солвера).
    """
    return bool(
        lot_canonical
        and (
            lot_canonical == canonical
            or set(lot_canonical.split()) & set(canonical.split())
        )
    )


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
        # TZ-M8 §3.6–3.7: сезонность и ротация по истории семьи
        "recency_penalty", "season_bonus", "time_minutes",
        # TZ-M8 §4: вкус семьи вместо одной звезды на рецепт
        "affinity", "unknown",
        # TZ-M8 §6.1: БЖУ на семью и белковая база блюда
        "protein_g", "fat_g", "carb_g", "protein_base",
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
        #: 1.0 — ели вчера, 0.0 — три недели не было (или не было вовсе)
        self.recency_penalty = 0.0
        #: доля сезонных продуктов в блюде
        self.season_bonus = 0.0
        #: время приготовления по книге, если оно известно
        self.time_minutes: int | None = None
        #: вкус семьи к блюду в шкале [-1, 1]; 0 — мнения нет
        self.affinity = 0.0
        #: семья не имеет об этом блюде никакого мнения
        self.unknown = True
        #: белок блюда на семью, граммы; None — оценить не по чему
        self.protein_g: int | None = None
        self.fat_g: int | None = None
        self.carb_g: int | None = None
        #: белковая база блюда (features.PROTEIN_BASES)
        self.protein_base: str = "veg"


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


def _stock_lots(
    inventory: list[dict[str, Any]],
    synonyms: Synonyms,
    normal: Any,
    base_quantity: Any,
) -> list[tuple[str, str | None, Decimal]]:
    """Запасы в базовых единицах: (каноническое имя, единица, количество)."""
    lots: list[tuple[str, str | None, Decimal]] = []
    for lot in inventory:
        canonical = synonyms.canonical_name(str(lot.get("name") or ""), normal)
        if not canonical:
            continue
        quantity = lot.get("quantity")
        if quantity is None:
            continue
        amount, unit = base_quantity(Decimal(str(quantity)), lot.get("unit_code"))
        if amount is None or amount <= 0:
            continue
        lots.append((canonical, unit, amount))
    return lots


def stock_available(
    lots: list[tuple[str, str | None, Decimal]], canonical: str, unit: str | None
) -> Decimal:
    """Сколько такого продукта лежит дома в этой единице.

    Кандидаты оцениваются независимо друг от друга, поэтому запас здесь не
    «списывается»: он показывает каждому блюду, сколько ему не придётся
    покупать. Реальное списание FEFO делает ``shopping.build_shopping`` уже
    по выбранному плану.
    """
    total = Decimal("0")
    for lot_canonical, lot_unit, amount in lots:
        if lot_unit == unit and canonical_overlap(lot_canonical, canonical):
            total += amount
    return total


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
    macros_hint: Any = None,
    base_quantity: Any = None,
    scale_of: Any = None,
) -> dict[int, CandidateScore]:
    """Считает оценку каждого кандидата один раз (TZ-M5R §2.2).

    ``normal``/``tokens``/``cost_hint``/``meal_score`` инъектируются из
    ``planner.py``, чтобы не создавать циклический импорт.

    TZ-M8 T1 (дефект P6): количества приводятся к порциям семьи через
    ``scale_of(recipe)``, и стоимость, и калории считаются уже на семью, а
    запасы вычитаются по количеству — раньше найденный дома продукт обнулял
    цену ингредиента целиком, сколько бы его ни требовалось.
    """
    soft_terms = soft_rule_terms(rules, synonyms, normal)
    expiring = _expiring_canonicals(inventory, starts_on, synonyms, normal)
    cuisine_set = set(cuisines)
    stock = _stock_lots(inventory, synonyms, normal, base_quantity) if base_quantity else []

    scores: dict[int, CandidateScore] = {}
    raw_costs: dict[int, tuple[int, int]] = {}  # id -> (стоимость, непокрытых)
    all_item_costs: list[int] = []  # цены отдельных сопоставленных ингредиентов
    for recipe in candidates:
        recipe_id = int(recipe["id"])
        score = CandidateScore(recipe_id)
        score.draft = recipe.get("review_status") != "ready"
        # У рецепта может быть несколько кухонь (TZ-M8): бонус даёт любое
        # пересечение с выбором семьи; 'universal' бонуса не даёт — он лишь
        # не мешает блюду попасть в пул.
        score.cuisine_bonus = (
            1
            if cuisine_set
            and {str(code) for code in json_list(recipe.get("cuisine_codes"))
                 or ([recipe["cuisine_code"]] if recipe.get("cuisine_code") else [])}
            & cuisine_set
            else 0
        )
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

        # Масштаб на семью: и цена, и калории считаются для тех порций, которые
        # реально будут приготовлены. Рецепт без порций в книге не
        # масштабируется (scale_unknown, ``scaling.recipe_scale``).
        scale = scale_of(recipe) if scale_of is not None else None
        if scale is None:
            scale = Decimal("1")
        kcal_total = Decimal("0")
        kcal_known = False
        macros_total = {"protein": Decimal("0"), "fat": Decimal("0"), "carb": Decimal("0")}
        macros_known = False
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
            unit = ingredient.get("unit_code")
            # «По вкусу» не масштабируется: соль на четверых не становится
            # «по вкусу ×2» (то же правило в ``scaling.scaled_quantity``).
            if quantity_decimal is not None and not ingredient.get("is_to_taste"):
                needed = quantity_decimal * scale
            else:
                needed = quantity_decimal
            # K2/N2: словоформы («масла», «муки») приводятся к канону, иначе
            # substring-справочник ккал молча промахивается. macros_hint —
            # инъекция из planner: сначала таблица ingredient_nutrition.
            # TZ-M8 §6.2: белок кандидата нужен целевой функции, поэтому
            # подсказка отдаёт не одни калории, а КБЖУ.
            if macros_hint is not None:
                kcal, protein, fat, carb = macros_hint(name, canonical, needed, unit)
            else:
                kcal = ingredient_kcal(canonical or name, needed, unit)
                protein = fat = carb = None
            if kcal is not None:
                kcal_total += kcal
                kcal_known = True
            for key, value in (("protein", protein), ("fat", fat), ("carb", carb)):
                if value is not None:
                    macros_total[key] += value
                    macros_known = True
            needed_base, unit_base = (
                base_quantity(needed, unit) if base_quantity is not None else (None, None)
            )
            remaining = needed_base
            if needed_base is not None and stock and canonical:
                # P6: дома есть 200 мл молока, а нужно 900 — покупаются 700.
                have = stock_available(stock, canonical, unit_base)
                if have > 0:
                    remaining = max(Decimal("0"), needed_base - have)
                    if remaining <= 0:
                        continue  # хватает запасов — эта позиция не покупается
            item_cost = cost_hint(ingredient, remaining, unit_base)
            if item_cost is None:
                unmatched += 1
            else:
                matched_costs.append(item_cost)
        if kcal_known:
            score.kcal = int(kcal_total)
        if macros_known:
            score.protein_g = int(macros_total["protein"])
            score.fat_g = int(macros_total["fat"])
            score.carb_g = int(macros_total["carb"])
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
