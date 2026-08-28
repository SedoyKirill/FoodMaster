"""КБЖУ из карточек Ленты и бонус рейтинга в целевой функции."""

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import _meal_nutrition
from app.web.planning.candidates import CandidateScore
from app.web.planning.optimizer import W_RATING, slot_coefficient


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


class RatingBonusTests(unittest.TestCase):
    def test_higher_rating_lowers_penalty(self) -> None:
        loved = CandidateScore(1)
        loved.meal_fit = {"dinner": 1.0}
        loved.rating_bonus = 2  # 5 звёзд
        disliked = CandidateScore(2)
        disliked.meal_fit = {"dinner": 1.0}
        disliked.rating_bonus = -2  # 1 звезда
        neutral = CandidateScore(3)
        neutral.meal_fit = {"dinner": 1.0}
        self.assertLess(
            slot_coefficient(loved, "dinner", 10),
            slot_coefficient(neutral, "dinner", 10),
        )
        self.assertGreater(
            slot_coefficient(disliked, "dinner", 10),
            slot_coefficient(neutral, "dinner", 10),
        )
        self.assertEqual(
            slot_coefficient(neutral, "dinner", 10) - slot_coefficient(loved, "dinner", 10),
            2 * W_RATING,
        )


if __name__ == "__main__":
    unittest.main()
