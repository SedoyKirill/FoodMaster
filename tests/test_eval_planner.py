"""Метрики офлайн-оценки планировщика (TZ-M8 §9.2).

Отчёт по этим числам решает, как двигать веса, поэтому сама арифметика
должна быть проверена: «60 % новизны» из неверной формулы хуже, чем
отсутствие метрики.
"""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "eval_planner",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "eval_planner.py",
    ),
)
evaluate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(evaluate)


def meal(recipe_id: int, day: str, meal_type: str, **fields) -> dict:
    return {
        "recipe_id": recipe_id,
        "meal_date": date.fromisoformat(day),
        "meal_type": meal_type,
        **fields,
    }


class NoveltyTests(unittest.TestCase):
    def test_share_of_dishes_absent_from_history(self) -> None:
        plan = {"meals": [meal(1, "2026-09-01", "dinner"), meal(2, "2026-09-02", "dinner")]}
        history = [{"recipe_id": 1}]
        self.assertEqual(evaluate.novelty(plan, history), 0.5)

    def test_empty_plan_is_not_novel(self) -> None:
        self.assertEqual(evaluate.novelty({"meals": []}, []), 0.0)

    def test_family_without_history_gets_full_novelty(self) -> None:
        plan = {"meals": [meal(7, "2026-09-01", "dinner")]}
        self.assertEqual(evaluate.novelty(plan, []), 1.0)


class CostTests(unittest.TestCase):
    def test_price_of_a_thousand_calories(self) -> None:
        plan = {
            "estimated_cost_kop": 200000,
            "meals": [
                meal(1, "2026-09-01", "dinner", estimated_kcal=1000),
                meal(2, "2026-09-01", "lunch", estimated_kcal=1000),
            ],
        }
        self.assertEqual(evaluate.cost_per_1000_kcal(plan), 1000.0)

    def test_plan_without_calorie_estimates_has_no_metric(self) -> None:
        """Делить на ноль калорий нельзя, и подставлять единицу тоже."""
        plan = {"estimated_cost_kop": 100, "meals": [meal(1, "2026-09-01", "dinner")]}
        self.assertIsNone(evaluate.cost_per_1000_kcal(plan))


class ProteinGapTests(unittest.TestCase):
    PEOPLE = [
        {"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1"),
         "target_kcal": 2000},
    ]

    def _plan(self, protein: int) -> dict:
        return {
            "meals": [
                meal(1, "2026-09-01", "breakfast", estimated_protein=protein),
                meal(2, "2026-09-01", "lunch", estimated_protein=protein),
                meal(3, "2026-09-01", "dinner", estimated_protein=protein),
            ]
        }

    def test_day_far_below_the_norm_counts_as_failed(self) -> None:
        self.assertEqual(evaluate.protein_gap(self._plan(1), self.PEOPLE), 1.0)

    def test_day_within_the_norm_does_not(self) -> None:
        self.assertEqual(evaluate.protein_gap(self._plan(80), self.PEOPLE), 0.0)

    def test_without_macro_estimates_the_metric_is_unknown(self) -> None:
        plan = {"meals": [meal(1, "2026-09-01", "dinner")]}
        self.assertIsNone(evaluate.protein_gap(plan, self.PEOPLE))


class TasteDeltaTests(unittest.TestCase):
    def test_family_without_events_has_no_taste_metric(self) -> None:
        """«Модель не отличает» и «модель не обучалась» — разные утверждения."""
        data = {"taste_events": [], "recipes": [], "people": []}
        self.assertIsNone(
            evaluate.taste_delta({"meals": []}, data, date(2026, 9, 1), seed=1)
        )


class FormattingTests(unittest.TestCase):
    def test_missing_values_are_dashes_not_zeroes(self) -> None:
        self.assertEqual(evaluate._percent(None), "—")
        self.assertEqual(evaluate._signed(None), "—")
        self.assertEqual(evaluate._money(None), "—")

    def test_taste_delta_keeps_its_sign(self) -> None:
        self.assertEqual(evaluate._signed(0.25), "+0.25")
        self.assertEqual(evaluate._signed(-0.25), "-0.25")


if __name__ == "__main__":
    unittest.main()
