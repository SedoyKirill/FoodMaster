"""Объяснение выбора блюда (TZ-M8 §5)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planning.candidates import CandidateScore  # noqa: E402
from app.web.planning.explain import (  # noqa: E402
    MAX_REASONS, ExplainContext, contributions, explain, main_reason,
)


def score_for(**fields) -> CandidateScore:
    score = CandidateScore(fields.pop("recipe_id", 1))
    score.cost_kop = fields.pop("cost_kop", 20000)
    score.meal_fit = {"dinner": fields.pop("meal_fit", 1.0)}
    score.meal_bias = {"dinner": 0.0}
    for name, value in fields.items():
        setattr(score, name, value)
    return score


class ContributionTests(unittest.TestCase):
    """Причины считаются по тем же весам, что и само решение."""

    def test_expiring_stock_shows_as_negative_contribution(self) -> None:
        parts = contributions(score_for(expiry_bonus=2), "dinner", 10)
        self.assertLess(parts["waste"], 0)

    def test_recent_dish_contributes_against_itself(self) -> None:
        parts = contributions(score_for(recency_penalty=1.0), "dinner", 10)
        self.assertGreater(parts["recency"], 0)


class ExplainTests(unittest.TestCase):
    def _codes(self, reasons: list[dict]) -> list[str]:
        return [reason["code"] for reason in reasons]

    def test_expiring_ingredient_is_the_reason(self) -> None:
        reasons = explain(
            score_for(expiry_bonus=1),
            ExplainContext(meal_type="dinner", expiring_names=("сметана",)),
        )
        self.assertIn("uses_expiring", self._codes(reasons))
        self.assertEqual(reasons[0]["ingredients"], ["сметана"])

    def test_family_rating_explains_the_choice(self) -> None:
        reasons = explain(
            score_for(affinity=0.9),
            ExplainContext(meal_type="dinner", rating=5),
        )
        self.assertIn("favorite", self._codes(reasons))

    def test_taste_for_the_dish_type_is_named_when_the_dish_is_new(self) -> None:
        """Мнения о блюде нет, но супы семья выбирает часто (§4.2)."""
        reasons = explain(
            score_for(affinity=0.4),
            ExplainContext(meal_type="dinner", dish_type="soup"),
        )
        self.assertIn("liked_type", self._codes(reasons))

    def test_cheaper_than_the_slot_median_is_reported_in_rubles(self) -> None:
        reasons = explain(
            score_for(cost_kop=10000),
            ExplainContext(meal_type="dinner", median_cost_kop=25000),
        )
        cheap = next(r for r in reasons if r["code"] == "cheap_today")
        self.assertEqual(cheap["delta_rub"], 150)

    def test_small_price_difference_is_not_a_reason(self) -> None:
        reasons = explain(
            score_for(cost_kop=24000),
            ExplainContext(meal_type="dinner", median_cost_kop=25000),
        )
        self.assertNotIn("cheap_today", self._codes(reasons))

    def test_stock_and_quick_are_added_outside_the_objective(self) -> None:
        reasons = explain(
            score_for(time_minutes=20),
            ExplainContext(meal_type="dinner", stock_names=("рис", "лук")),
        )
        codes = self._codes(reasons)
        self.assertIn("uses_stock", codes)
        self.assertIn("quick", codes)

    def test_new_dish_is_offered_as_new(self) -> None:
        reasons = explain(
            score_for(), ExplainContext(meal_type="dinner", known=False)
        )
        self.assertIn("new_for_you", self._codes(reasons))

    def test_long_forgotten_dish_is_explained_by_rotation(self) -> None:
        reasons = explain(
            score_for(), ExplainContext(meal_type="dinner", days_since=20)
        )
        self.assertIn("rotation", self._codes(reasons))

    def test_recently_eaten_dish_does_not_claim_rotation(self) -> None:
        reasons = explain(
            score_for(), ExplainContext(meal_type="dinner", days_since=2)
        )
        self.assertNotIn("rotation", self._codes(reasons))

    def test_kcal_fit_when_dish_matches_the_slot_target(self) -> None:
        reasons = explain(
            score_for(kcal=800), ExplainContext(meal_type="dinner", kcal_target=850)
        )
        self.assertIn("kcal_fit", self._codes(reasons))

    def test_there_is_always_at_least_one_reason(self) -> None:
        reasons = explain(score_for(meal_fit=0.0), ExplainContext(meal_type="dinner"))
        self.assertEqual(self._codes(reasons), ["fits_meal"])

    def test_no_more_than_three_reasons(self) -> None:
        reasons = explain(
            score_for(expiry_bonus=2, affinity=0.9, cost_kop=5000, time_minutes=15),
            ExplainContext(
                meal_type="dinner", median_cost_kop=40000, rating=5,
                expiring_names=("сметана",), stock_names=("рис",), days_since=21,
            ),
        )
        self.assertLessEqual(len(reasons), MAX_REASONS)

    def test_main_reason_is_the_strongest_one(self) -> None:
        reason = main_reason(
            score_for(expiry_bonus=3),
            ExplainContext(meal_type="dinner", expiring_names=("творог",)),
        )
        self.assertEqual(reason["code"], "uses_expiring")


if __name__ == "__main__":
    unittest.main()
