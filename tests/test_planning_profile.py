"""Профиль едоков: нормы ккал/БЖУ, приёмы дома, личные правила (TZ-M8 §3.1–3.2)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planning.profile import (  # noqa: E402
    DEFAULT_MEAL_SHARES, MEAL_TYPES, daily_target, eaters_of, rule_terms_for_meal,
    slot_servings,
)
from fixtures import make_person  # noqa: E402


TODAY = date(2026, 8, 28)


class AdultTargetTests(unittest.TestCase):
    """Взрослый: ручная цель → формула → честная константа."""

    def test_manual_target_wins_over_formula(self) -> None:
        person = make_person(
            target_kcal=1800, sex="female", height_cm=Decimal("165"),
            weight_kg=Decimal("60"), birth_date=date(1990, 5, 1),
        )
        target = daily_target(person, TODAY)
        self.assertEqual(target.kcal, 1800)
        self.assertEqual(target.source, "manual")

    def test_mifflin_formula_used_when_measurements_known(self) -> None:
        # Миффлин–Сан-Жеор: 10·80 + 6.25·180 − 5·36 + 5 = 1750; ×1.45 ≈ 2537.
        person = make_person(
            sex="male", height_cm=Decimal("180"), weight_kg=Decimal("80"),
            birth_date=date(1990, 5, 1), activity="moderate",
        )
        target = daily_target(person, TODAY)
        self.assertEqual(target.source, "formula")
        self.assertAlmostEqual(target.kcal, 2537, delta=5)

    def test_goal_lose_cuts_fifteen_percent(self) -> None:
        person = make_person(
            sex="male", height_cm=Decimal("180"), weight_kg=Decimal("80"),
            birth_date=date(1990, 5, 1), activity="moderate", goal="lose",
        )
        maintain = daily_target(make_person(
            sex="male", height_cm=Decimal("180"), weight_kg=Decimal("80"),
            birth_date=date(1990, 5, 1), activity="moderate",
        ), TODAY)
        self.assertAlmostEqual(
            daily_target(person, TODAY).kcal, int(maintain.kcal * 0.85), delta=2
        )

    def test_without_measurements_falls_back_to_constant(self) -> None:
        target = daily_target(make_person(), TODAY)
        self.assertEqual(target.kcal, 2000)
        self.assertEqual(target.source, "default")


class ChildTargetTests(unittest.TestCase):
    """Ребёнку дефицит не назначается (TZ-v2 §9), норма — по возрасту."""

    def test_child_norm_by_age(self) -> None:
        child = make_person(person_type="child", birth_date=date(2018, 3, 1))
        self.assertEqual(daily_target(child, TODAY).kcal, 1800)  # 8 лет

    def test_child_ignores_weight_loss_goal(self) -> None:
        child = make_person(
            person_type="child", birth_date=date(2018, 3, 1), goal="lose",
        )
        self.assertEqual(daily_target(child, TODAY).kcal, 1800)

    def test_child_without_birth_date_uses_constant(self) -> None:
        self.assertEqual(daily_target(make_person(person_type="child"), TODAY).kcal, 1400)


class MacroTests(unittest.TestCase):
    """БЖУ считаются из долей энергии, а не берутся с потолка."""

    def test_default_shares_split_energy(self) -> None:
        target = daily_target(make_person(target_kcal=2000), TODAY)
        self.assertEqual(target.protein_g, 100)  # 2000·0.20 / 4
        self.assertEqual(target.fat_g, 66)       # 2000·0.30 / 9
        self.assertEqual(target.carb_g, 250)     # 2000·0.50 / 4

    def test_custom_shares_are_respected(self) -> None:
        person = make_person(
            target_kcal=2000, protein_share=Decimal("0.4"),
            fat_share=Decimal("0.3"), carb_share=Decimal("0.3"),
        )
        self.assertEqual(daily_target(person, TODAY).protein_g, 200)


class MealShareTests(unittest.TestCase):
    """Цель приёма — доля дневной; приёмы вне дома не планируются."""

    def test_by_meal_uses_default_shares(self) -> None:
        target = daily_target(make_person(target_kcal=2000), TODAY)
        self.assertEqual(target.by_meal["lunch"], int(2000 * DEFAULT_MEAL_SHARES["lunch"]))
        self.assertEqual(set(target.by_meal), set(MEAL_TYPES))

    def test_meal_eaten_outside_home_drops_from_target(self) -> None:
        person = make_person(target_kcal=2000, eats_meals=["breakfast", "dinner"])
        target = daily_target(person, TODAY)
        self.assertNotIn("lunch", target.by_meal)


class SlotEatersTests(unittest.TestCase):
    """Порции слота считаются по тем, кто дома (TZ-M8 §3.1)."""

    PEOPLE = [
        make_person(name="Мама", portion_factor=Decimal("1")),
        make_person(name="Папа", portion_factor=Decimal("1"), eats_meals=["breakfast", "dinner"]),
        make_person(name="Дочь", person_type="child", portion_factor=Decimal("0.65")),
    ]

    def test_lunch_counts_only_people_at_home(self) -> None:
        self.assertEqual([p["name"] for p in eaters_of(self.PEOPLE, "lunch")], ["Мама", "Дочь"])
        self.assertEqual(slot_servings(self.PEOPLE, "lunch"), Decimal("1.65"))

    def test_dinner_counts_everyone(self) -> None:
        self.assertEqual(slot_servings(self.PEOPLE, "dinner"), Decimal("2.65"))

    def test_empty_slot_never_gives_zero_servings(self) -> None:
        loners = [make_person(eats_meals=["dinner"])]
        self.assertEqual(slot_servings(loners, "lunch"), Decimal("0"))


class PersonalRuleTests(unittest.TestCase):
    """Личное правило действует на слот, только если человек его ест дома."""

    def setUp(self) -> None:
        self.child = make_person(name="Дочь", person_type="child")
        self.father = make_person(name="Папа", eats_meals=["breakfast", "dinner"])
        self.people = [self.child, self.father]

    def test_family_rule_applies_to_every_meal(self) -> None:
        rules = [{"rule_type": "allergy", "term": "орехи", "is_hard": True, "person_id": None}]
        for meal in MEAL_TYPES:
            hard, _tags = rule_terms_for_meal(rules, self.people, meal)
            self.assertEqual([rule["term"] for rule in hard], ["орехи"])

    def test_personal_rule_binds_only_to_meals_at_home(self) -> None:
        rules = [{
            "rule_type": "allergy", "term": "орехи", "is_hard": True,
            "person_id": self.father["id"],
        }]
        hard_lunch, _ = rule_terms_for_meal(rules, self.people, "lunch")
        hard_dinner, _ = rule_terms_for_meal(rules, self.people, "dinner")
        self.assertEqual(hard_lunch, [])
        self.assertEqual([rule["term"] for rule in hard_dinner], ["орехи"])

    def test_diet_tag_collected_per_slot(self) -> None:
        rules = [{
            "rule_type": "exclude", "term": "мясо", "is_hard": True,
            "person_id": self.child["id"], "diet_tag": "vegetarian",
        }]
        _hard, tags = rule_terms_for_meal(rules, self.people, "lunch")
        self.assertEqual(tags, {"vegetarian"})

    def test_rule_of_unknown_person_is_treated_as_family_wide(self) -> None:
        """Человека удалили, правило осталось — оно не должно тихо исчезнуть."""
        rules = [{
            "rule_type": "allergy", "term": "орехи", "is_hard": True,
            "person_id": "00000000-0000-0000-0000-000000000000",
        }]
        hard, _tags = rule_terms_for_meal(rules, self.people, "lunch")
        self.assertEqual([rule["term"] for rule in hard], ["орехи"])


if __name__ == "__main__":
    unittest.main()
