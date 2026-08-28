"""Тесты фиксов аудита 18.08.2026: K1 (медиана импутации цены), K2 (ккал в
масштабе семьи), K4 (персистентность solver_status/предупреждений), N2
(покрытие ккал и словоформы через словарь синонимов)."""

import asyncio
import os
import sys
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool, repository_with_pool
from fixtures import make_ingredient, make_person, make_recipe

from app.web.planner import _meal_nutrition, build_plan
from app.web.planning.candidates import Synonyms, score_candidates


def _normal(text: str) -> str:
    return str(text).lower().strip()


def _tokens(text: str):
    return _normal(text).split()


class K1MedianImputationTests(unittest.TestCase):
    """K1: медиана — по ценам отдельных ингредиентов, а не «сумма ÷ пул»."""

    @staticmethod
    def _make_pool(size: int) -> list[dict]:
        recipes = []
        for recipe_id in range(1, size + 1):
            recipes.append(
                make_recipe(
                    id=recipe_id,
                    ingredients=[
                        make_ingredient(
                            normalized_name=f"продукт-{recipe_id}",
                            unit_code="piece", quantity_min=Decimal("1"),
                            quantity_max=Decimal("1"),
                        ),
                        make_ingredient(
                            normalized_name=f"экзотика-{recipe_id}",
                            unit_code="piece", quantity_min=Decimal("1"),
                            quantity_max=Decimal("1"),
                        ),
                    ],
                )
            )
        return recipes

    def _scores(self, pool_size: int):
        return score_candidates(
            self._make_pool(pool_size),
            meal_types=["dinner"], cuisines=[], rules=[], inventory=[],
            starts_on=date(2026, 8, 18), synonyms=Synonyms(), normal=_normal,
            tokens=_tokens,
            cost_hint=lambda ing, needed, unit: (
                10_000 if str(ing.get("normalized_name", "")).startswith("продукт") else None
            ),
            meal_score=lambda recipe, meal_type: 0,
        )

    def test_k1_imputation_does_not_depend_on_pool_size(self) -> None:
        for pool_size in (1, 10, 100):
            scores = self._scores(pool_size)
            self.assertEqual(
                scores[1].cost_kop, 20_000,
                f"при пуле {pool_size} несопоставленный ингредиент должен "
                f"стоить медианные 10000 коп",
            )


class K2SolverKcalScaleTests(unittest.TestCase):
    """K2: солвер получает ккал в масштабе семьи, а не «как в книге»."""

    def test_k2_score_kcal_scaled_to_desired_servings(self) -> None:
        # Рецепт на 2 порции, молоко 200 мл → 104 ккал в базе.
        recipe = make_recipe(
            id=1,
            source_servings_min=Decimal("2"),
            ingredients=[make_ingredient()],  # молоко, 200 мл
        )
        people = [make_person() for _ in range(4)]  # 4 порции → scale 2
        captured: dict = {}

        def spy_optimize(**kwargs):
            captured["scores"] = kwargs["scores"]
            return {}, "greedy"

        with patch("app.web.planning.optimizer.optimize", side_effect=spy_optimize):
            build_plan(
                household_id="h", starts_on=date(2026, 8, 18), days=1,
                cuisines=[], people=people, appliances=[], rules=[],
                inventory=[], recipes=[recipe], products=[],
            )
        self.assertEqual(captured["scores"][1].kcal, 208)  # 104 × 2


class N2KcalCoverageTests(unittest.TestCase):
    """N2: покрытие ккал по ингредиентам и словоформы через синонимы."""

    class _NoMatcher:
        def match(self, name, unit, tier, quantity):
            return None

    def test_n2_wordform_resolved_via_synonyms(self) -> None:
        synonyms = Synonyms(forms={"масла": "масло"})
        ingredients = [
            make_ingredient(
                normalized_name="масла", unit_code="g",
                quantity_min=Decimal("100"), quantity_max=Decimal("100"),
            ),
        ]
        result = _meal_nutrition(
            ingredients, Decimal("1"), self._NoMatcher(), "balanced",
            synonyms=synonyms, normal=_normal,
        )
        self.assertEqual(result["estimated_kcal"], 899)  # правило «масло»
        self.assertEqual(result["kcal_coverage"], (1, 1))

    def test_n2_coverage_counts_unknown_ingredients(self) -> None:
        ingredients = [
            make_ingredient(),  # молоко — известно
            make_ingredient(
                normalized_name="хирёдзу", unit_code="g",
                quantity_min=Decimal("50"), quantity_max=Decimal("50"),
            ),
            make_ingredient(
                normalized_name="соль", unit_code=None, quantity_min=None,
                quantity_max=None, is_to_taste=True,
            ),
        ]
        result = _meal_nutrition(
            ingredients, Decimal("1"), self._NoMatcher(), "balanced",
            synonyms=Synonyms(), normal=_normal,
        )
        # «по вкусу» не считается в знаменателе, неизвестный — считается.
        self.assertEqual(result["kcal_coverage"], (1, 2))


class K4PersistenceTests(unittest.TestCase):
    """K4: solver_status и предупреждения плана переживают перезагрузку."""

    SESSION = {"household_id": "h-1", "user_id": "u-1", "role": "owner"}

    def test_k4_save_plan_writes_solver_status_and_warnings(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        plan = {
            "estimated_cost_kop": 100, "matched_cost_items": 1,
            "total_cost_items": 1, "meals": [], "shopping": [],
            "solver_status": "FEASIBLE",
            "warnings": ["budget_exceeded: бюджет превышен на 5 ₽."],
        }
        asyncio.run(
            repository.save_plan(
                self.SESSION, date(2026, 8, 18), 3, None, [], "balanced", plan
            )
        )
        sql, args = pool.first_matching("INSERT INTO app_core.meal_plans")
        self.assertIn("solver_status", sql)
        self.assertIn("plan_warnings", sql)
        self.assertIn("FEASIBLE", args)
        self.assertTrue(any("budget_exceeded" in str(arg) for arg in args))

    def test_k4_plan_payload_returns_stored_warnings(self) -> None:
        pool = FakePool()
        pool.on("fetch", "FROM app_core.plan_meals", [])
        pool.on("fetch", "FROM app_core.plan_ingredients", [])
        repository = repository_with_pool(pool)
        header = {
            "id": "p-1", "starts_on": "2026-08-18", "days": 3,
            "budget_kop": None, "estimated_cost_kop": 100,
            "matched_cost_items": 1, "total_cost_items": 1,
            "cuisine_preferences": "[]", "price_tier": "balanced",
            "status": "draft", "created_at": "2026-08-18T10:00:00+03:00",
            "solver_status": "greedy",
            "plan_warnings": '["budget_exceeded: бюджет превышен на 5 ₽."]',
        }
        payload = asyncio.run(repository._plan_payload(header))
        self.assertEqual(payload["solver_status"], "greedy")
        self.assertEqual(
            payload["warnings"], ["budget_exceeded: бюджет превышен на 5 ₽."]
        )


if __name__ == "__main__":
    unittest.main()
