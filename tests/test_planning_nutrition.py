"""КБЖУ из карточек Ленты и бонус рейтинга в целевой функции."""

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import _meal_nutrition
from app.web.planning.candidates import CandidateScore
from app.web.planning.optimizer import slot_coefficient
from app.web.planning.weights import weights_for


class _StubMatcher:
    """Матчер, отдающий один товар с известным КБЖУ на 100 г."""

    def match(self, name, unit, tier, quantity):
        if "хирёдзу" in name:
            return None
        return {
            "id": 1,
            "name": "Товар",
            "kcal_100": Decimal("200"),
            "protein_100": Decimal("10"),
            "fat_100": Decimal("5"),
            "carb_100": Decimal("30"),
            "effective_price_kop": 10000,
            "pack_quantity": Decimal("500"),
            "pack_unit": "g",
        }


class MealNutritionTests(unittest.TestCase):
    def test_macros_come_from_catalog_card(self) -> None:
        ingredients = [
            {"normalized_name": "тестопродукт", "quantity_min": Decimal("300"), "unit_code": "g"},
        ]
        result = _meal_nutrition(ingredients, Decimal("1"), _StubMatcher(), "balanced")
        self.assertEqual(result["estimated_kcal"], 600)  # 200 ккал × 3
        self.assertEqual(result["estimated_protein"], 30)
        self.assertEqual(result["estimated_fat"], 15)
        self.assertEqual(result["estimated_carb"], 90)

    def test_unmatched_and_piece_units_stay_unknown(self) -> None:
        ingredients = [
            {"normalized_name": "хирёдзу", "quantity_min": Decimal("100"), "unit_code": "g"},
            {"normalized_name": "яблоко", "quantity_min": Decimal("2"), "unit_code": "piece"},
        ]
        result = _meal_nutrition(ingredients, Decimal("1"), _StubMatcher(), "balanced")
        self.assertIsNone(result["estimated_protein"])

    def test_scale_multiplies_macros(self) -> None:
        ingredients = [
            {"normalized_name": "тестопродукт", "quantity_min": Decimal("100"), "unit_code": "g"},
        ]
        result = _meal_nutrition(ingredients, Decimal("2"), _StubMatcher(), "balanced")
        self.assertEqual(result["estimated_kcal"], 400)


class TasteAffinityTests(unittest.TestCase):
    """Вкус семьи в целевой функции (TZ-M8 §4): звёзды — лишь одно событие."""

    @staticmethod
    def _score(recipe_id: int, affinity: float) -> CandidateScore:
        score = CandidateScore(recipe_id)
        score.meal_fit = {"dinner": 1.0}
        score.affinity = affinity
        score.unknown = False
        return score

    def test_loved_dish_beats_neutral_and_disliked(self) -> None:
        weights = weights_for("balanced")
        loved = self._score(1, 1.0)
        neutral = self._score(2, 0.0)
        disliked = self._score(3, -1.0)
        self.assertLess(
            slot_coefficient(loved, "dinner", weights),
            slot_coefficient(neutral, "dinner", weights),
        )
        self.assertGreater(
            slot_coefficient(disliked, "dinner", weights),
            slot_coefficient(neutral, "dinner", weights),
        )
        self.assertEqual(
            slot_coefficient(neutral, "dinner", weights)
            - slot_coefficient(loved, "dinner", weights),
            weights.taste,
        )

    def test_unknown_dish_is_slightly_behind_a_known_neutral_one(self) -> None:
        weights = weights_for("balanced")
        known = self._score(1, 0.0)
        unknown = self._score(2, 0.0)
        unknown.unknown = True
        self.assertEqual(
            slot_coefficient(unknown, "dinner", weights)
            - slot_coefficient(known, "dinner", weights),
            weights.unknown,
        )


if __name__ == "__main__":
    unittest.main()
