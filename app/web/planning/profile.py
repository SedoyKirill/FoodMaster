"""Профиль едоков: нормы ккал/БЖУ, приёмы дома, личные правила (TZ-M8 §3.1–3.2).

Планировщик до M8 знал про человека две вещи: сколько он ест порций и
(необязательно) цель по калориям. Аллергия ребёнка запрещала блюдо всей семье
на все приёмы, обед человека, который обедает на работе, всё равно готовился
на него, а БЖУ считались, но ни на что не влияли.

Здесь считается норма каждого едока (ручная цель → формула → честная
константа), раскладывается по приёмам пищи и собираются правила, которые
действуют на конкретный слот. Никакой медицины: формулы бытовые, значения
округляются, источник нормы всегда виден пользователю (``target_source``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ..planner import DEFAULT_TARGET_KCAL, json_list

#: приёмы пищи, которые планирует «Рацион» (TZ-v2: завтрак, обед, ужин)
MEAL_TYPES = ("breakfast", "lunch", "dinner")

#: доля дневной нормы на приём, если семья не задала свою
DEFAULT_MEAL_SHARES = {"breakfast": 0.25, "lunch": 0.40, "dinner": 0.35}

#: доли энергии по белкам/жирам/углеводам — обычное бытовое соотношение
DEFAULT_MACRO_SHARES = {"protein": Decimal("0.25"), "fat": Decimal("0.30"), "carb": Decimal("0.45")}

#: ккал в грамме нутриента (Атватер)
KCAL_PER_GRAM = {"protein": Decimal("4"), "fat": Decimal("9"), "carb": Decimal("4")}

#: множитель активности к основному обмену
ACTIVITY_FACTORS = {"low": Decimal("1.2"), "moderate": Decimal("1.45"), "high": Decimal("1.7")}

#: поправка к норме под цель; ребёнку дефицит не назначается (TZ-v2 §9)
GOAL_FACTORS = {"maintain": Decimal("1"), "lose": Decimal("0.85"), "gain": Decimal("1.1")}

#: Ориентировочные суточные нормы для детей по возрасту, ккал. Источник —
#: «Нормы физиологических потребностей в энергии и пищевых веществах для
#: различных групп населения РФ» (МР 2.3.1.2432-08), усреднённо по полу.
#: Это ориентир для планирования меню, а не медицинское назначение.
CHILD_KCAL_NORMS = ((3, 1200), (6, 1500), (10, 1800), (13, 2200), (17, 2500))


@dataclass(frozen=True)
class PersonTarget:
    """Суточная цель одного едока и её раскладка по приёмам."""

    kcal: int
    protein_g: int
    fat_g: int
    carb_g: int
    source: str  # manual | formula | default
    by_meal: dict[str, int] = field(default_factory=dict)


def age_years(birth_date: Any, on_date: date) -> int | None:
    if not isinstance(birth_date, date):
        return None
    years = on_date.year - birth_date.year
    if (on_date.month, on_date.day) < (birth_date.month, birth_date.day):
        years -= 1
    return max(0, years)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _child_norm(age: int | None) -> int:
    if age is None:
        return DEFAULT_TARGET_KCAL["child"]
    for upper, kcal in CHILD_KCAL_NORMS:
        if age <= upper:
            return kcal
    return DEFAULT_TARGET_KCAL["adult"]


def _basal_kcal(person: dict[str, Any], on_date: date) -> Decimal | None:
    """Миффлин–Сан-Жеор; None, если мерок не хватает."""
    height = _decimal(person.get("height_cm"))
    weight = _decimal(person.get("weight_kg"))
    age = age_years(person.get("birth_date"), on_date)
    if height is None or weight is None or age is None:
        return None
    correction = Decimal("5") if person.get("sex") == "male" else Decimal("-161")
    return Decimal("10") * weight + Decimal("6.25") * height - Decimal("5") * age + correction


def macro_shares(person: dict[str, Any]) -> dict[str, Decimal]:
    """Доли энергии по БЖУ; неполный или бессмысленный набор — к дефолту."""
    shares = {
        key: _decimal(person.get(f"{key}_share")) or DEFAULT_MACRO_SHARES[key]
        for key in DEFAULT_MACRO_SHARES
    }
    total = sum(shares.values())
    if total <= 0:
        return dict(DEFAULT_MACRO_SHARES)
    # Доли нормируются: 40/30/30 и 0.4/0.3/0.3 должны значить одно и то же.
    return {key: value / total for key, value in shares.items()}


def meal_shares(person: dict[str, Any]) -> dict[str, Decimal]:
    """Доли дневной нормы по приёмам; хранится JSONB, приходит и строкой."""
    stored = person.get("meal_shares")
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = None
    default = {meal: Decimal(str(DEFAULT_MEAL_SHARES[meal])) for meal in MEAL_TYPES}
    if not isinstance(stored, dict) or not stored:
        return default
    shares = {
        meal: _decimal(stored.get(meal)) or default[meal]
        for meal in MEAL_TYPES
    }
    return shares if sum(shares.values()) > 0 else default


def eats_meals(person: dict[str, Any]) -> tuple[str, ...]:
    """Какие приёмы человек ест дома. Пусто/мусор — ест всё (как до M8)."""
    stored = person.get("eats_meals")
    values = [str(item) for item in json_list(stored)] if stored is not None else []
    chosen = tuple(meal for meal in MEAL_TYPES if meal in values)
    return chosen or MEAL_TYPES


def daily_target(person: dict[str, Any], on_date: date) -> PersonTarget:
    """Норма едока на день: ручная цель → формула → константа."""
    is_child = str(person.get("person_type") or "adult") == "child"
    manual = person.get("target_kcal")
    if manual:
        kcal, source = Decimal(str(manual)), "manual"
    elif is_child:
        kcal, source = Decimal(_child_norm(age_years(person.get("birth_date"), on_date))), "default"
    else:
        basal = _basal_kcal(person, on_date)
        if basal is None:
            kcal, source = Decimal(DEFAULT_TARGET_KCAL["adult"]), "default"
        else:
            activity = ACTIVITY_FACTORS.get(str(person.get("activity") or "moderate"), ACTIVITY_FACTORS["moderate"])
            goal = GOAL_FACTORS.get(str(person.get("goal") or "maintain"), GOAL_FACTORS["maintain"])
            kcal, source = basal * activity * goal, "formula"

    shares = macro_shares(person)
    grams = {
        key: int(kcal * shares[key] / KCAL_PER_GRAM[key]) for key in DEFAULT_MACRO_SHARES
    }
    per_meal = meal_shares(person)
    eaten = eats_meals(person)
    return PersonTarget(
        kcal=int(kcal),
        protein_g=grams["protein"],
        fat_g=grams["fat"],
        carb_g=grams["carb"],
        source=source,
        by_meal={meal: int(kcal * per_meal[meal]) for meal in MEAL_TYPES if meal in eaten},
    )


def eaters_of(people: list[dict[str, Any]], meal_type: str) -> list[dict[str, Any]]:
    """Кто ест этот приём дома."""
    return [person for person in people if meal_type in eats_meals(person)]


def slot_servings(people: list[dict[str, Any]], meal_type: str) -> Decimal:
    """Порции слота: сумма ``portion_factor`` только тех, кто дома.

    Раньше порции считались по всем членам семьи, и обед готовился в том числе
    на того, кто обедает на работе (``scaling.desired_servings``).
    """
    total = Decimal("0")
    for person in eaters_of(people, meal_type):
        factor = person.get("portion_factor")
        if factor is None:
            factor = "0.65" if person.get("person_type") == "child" else "1"
        total += Decimal(str(factor))
    return total


def slot_kcal_target(people: list[dict[str, Any]], meal_type: str, on_date: date) -> int:
    """Цель слота — сумма целей приёма у тех, кто его ест дома."""
    return sum(
        daily_target(person, on_date).by_meal.get(meal_type, 0)
        for person in eaters_of(people, meal_type)
    )


def slot_protein_target(people: list[dict[str, Any]], meal_type: str, on_date: date) -> int:
    """Белок на слот — доля дневной нормы белка едоков, пропорционально ккал."""
    total = 0
    for person in eaters_of(people, meal_type):
        target = daily_target(person, on_date)
        if not target.kcal:
            continue
        share = target.by_meal.get(meal_type, 0) / target.kcal
        total += int(target.protein_g * share)
    return total


def rule_terms_for_meal(
    rules: list[dict[str, Any]], people: list[dict[str, Any]], meal_type: str
) -> tuple[list[dict[str, Any]], set[str]]:
    """Правила, действующие на слот, и требуемые диет-теги.

    Готовим одно блюдо на всех едоков слота (TZ-M8 А1), поэтому жёсткое
    правило любого присутствующего запрещает блюдо целиком. Правило человека,
    который этот приём дома не ест, на слот не действует. Правило с
    ``person_id`` несуществующего человека считается семейным: тихо терять
    аллергию из-за удалённой карточки нельзя.
    """
    present = {str(person.get("id")) for person in eaters_of(people, meal_type)}
    known = {str(person.get("id")) for person in people}
    applicable: list[dict[str, Any]] = []
    diet_tags: set[str] = set()
    for rule in rules:
        person_id = rule.get("person_id")
        if person_id is not None and str(person_id) in known and str(person_id) not in present:
            continue
        applicable.append(rule)
        tag = rule.get("diet_tag")
        if tag:
            diet_tags.add(str(tag))
    return applicable, diet_tags
