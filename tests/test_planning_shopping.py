"""TZ-M5R §4, тесты 8–9: масштабирование порций и агрегация покупок."""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import _base_quantity, _normal, build_plan
from app.web.planning.candidates import Synonyms
from app.web.planning.scaling import (
    display_quantity, recipe_scale, scaled_quantity,
)
from app.web.planning.shopping import (
    aggregate_ingredients, build_shopping, prepare_inventory,
)

SYNONYMS = Synonyms.from_rows([
    {"term": "молока", "canonical": "молоко", "kind": "form"},
    {"term": "сахара", "canonical": "сахар", "kind": "form"},
])


class _NoMatcher:
    def match(self, *args, **kwargs):
        return None


class ScalingTests(unittest.TestCase):
    def test_scale_unknown_without_servings(self) -> None:
        """Тест 8: рецепт без порций не масштабируется и помечен."""
        scale, unknown = recipe_scale({"source_servings_min": None}, Decimal("3"))
        self.assertIsNone(scale)
        self.assertTrue(unknown)

    def test_scale_known(self) -> None:
        scale, unknown = recipe_scale({"source_servings_min": Decimal("2")}, Decimal("3"))
        self.assertEqual(scale, Decimal("1.5"))
        self.assertFalse(unknown)

    def test_to_taste_is_not_scaled(self) -> None:
        ingredient = {"quantity_min": Decimal("5"), "is_to_taste": True}
        self.assertEqual(scaled_quantity(ingredient, Decimal("2")), Decimal("5"))

    def test_unknown_scale_keeps_book_quantities(self) -> None:
        ingredient = {"quantity_min": Decimal("200")}
        self.assertEqual(scaled_quantity(ingredient, None), Decimal("200"))

    def test_display_rounding(self) -> None:
        self.assertEqual(display_quantity(Decimal("148"), "g"), Decimal("150"))
        self.assertEqual(display_quantity(Decimal("1.2"), "piece"), Decimal("2"))
        self.assertEqual(display_quantity(Decimal("333"), "ml"), Decimal("335"))

    def test_plan_marks_scale_unknown(self) -> None:
        recipes = [
            {
                "id": 1,
                "title": "Пирог без порций",
                "source_page_start": 1,
                "source_servings_min": None,
                "cuisine_code": None,
                "meal_types": ["dinner"],
                "appliances": [],
                "review_status": "needs_review",
                "extraction_confidence": Decimal("0.9"),
                "ingredients": [
                    {
                        "ingredient_text": "Мука",
                        "normalized_name": "мука",
                        "quantity_min": Decimal("400"),
                        "quantity_max": Decimal("400"),
                        "unit_code": "g",
                    }
                ],
            }
        ]
        plan = build_plan(
            household_id="household",
            starts_on=date(2026, 8, 17),
            days=1,
            cuisines=[],
            people=[
                {"name": "А", "person_type": "adult", "portion_factor": Decimal("1")},
                {"name": "Б", "person_type": "adult", "portion_factor": Decimal("1")},
            ],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=recipes,
            products=[],
        )
        meal = next(m for m in plan["meals"] if m["recipe_id"] == 1)
        self.assertTrue(meal["scale_unknown"])
        self.assertEqual(meal["scale"], Decimal("1"))
        self.assertTrue(any(w.startswith("scale_unknown") for w in plan["warnings"]))
        flour = next(i for i in plan["shopping"] if i["normalized_name"] == "мука")
        self.assertEqual(flour["quantity"], Decimal("400"))  # как в книге, ×1


class ShoppingTests(unittest.TestCase):
    def test_word_forms_and_units_merge(self) -> None:
        """Тест 9: «молоко» 300 мл + «молока» 0.5 л → одна позиция 800 мл."""
        aggregate = aggregate_ingredients(
            [
                {"name": "молоко", "quantity": Decimal("300"), "unit_code": "ml"},
                {"name": "молока", "quantity": Decimal("0.5"), "unit_code": "l"},
                {"name": "сахар", "quantity": Decimal("200"), "unit_code": "g"},
                {"name": "сахара", "quantity": Decimal("1"), "unit_code": "kg"},
            ],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        milk = aggregate[("молоко", "ml")]
        self.assertEqual(milk["quantity"], Decimal("800"))
        sugar = aggregate[("сахар", "g")]
        self.assertEqual(sugar["quantity"], Decimal("1200"))
        self.assertEqual(len(aggregate), 2)

    def test_fefo_uses_earliest_expiry_first(self) -> None:
        lots = prepare_inventory(
            [
                {"name": "Молоко", "quantity": Decimal("400"), "unit_code": "ml",
                 "expires_on": date(2026, 8, 25)},
                {"name": "Молоко", "quantity": Decimal("400"), "unit_code": "ml",
                 "expires_on": date(2026, 8, 18)},
            ],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        aggregate = aggregate_ingredients(
            [{"name": "молоко", "quantity": Decimal("500"), "unit_code": "ml"}],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        shopping, _, _ = build_shopping(
            aggregate, lots, _NoMatcher(), "balanced", _base_quantity
        )
        milk = shopping[0]
        self.assertEqual(milk["covered_from_inventory"], Decimal("500"))
        self.assertEqual(milk["buy_quantity"], Decimal("0"))
        early = next(lot for lot in lots if lot["expires_on"] == date(2026, 8, 18))
        late = next(lot for lot in lots if lot["expires_on"] == date(2026, 8, 25))
        self.assertEqual(early["base_quantity"], Decimal("0"))  # списан первым
        self.assertEqual(late["base_quantity"], Decimal("300"))

    def test_pack_count_and_leftover(self) -> None:
        class Matcher:
            def match(self, name, unit, tier, quantity):
                return {
                    "id": 7,
                    "name": "Молоко пастеризованное",
                    "pack_quantity": Decimal("930"),
                    "pack_unit": "ml",
                    "effective_price_kop": 8999,
                }

        aggregate = aggregate_ingredients(
            [{"name": "молоко", "quantity": Decimal("1000"), "unit_code": "ml"}],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        shopping, total, matched = build_shopping(
            aggregate, [], Matcher(), "balanced", _base_quantity
        )
        milk = shopping[0]
        self.assertEqual(milk["pack_count"], 2)
        self.assertEqual(milk["estimated_cost_kop"], 17998)
        self.assertEqual(milk["leftover_quantity"], Decimal("860"))
        self.assertEqual(total, 17998)
        self.assertEqual(matched, 1)

    def test_unmatched_position_is_flagged(self) -> None:
        aggregate = aggregate_ingredients(
            [{"name": "хирёдзу", "quantity": Decimal("100"), "unit_code": "g"}],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        shopping, total, _ = build_shopping(
            aggregate, [], _NoMatcher(), "balanced", _base_quantity
        )
        self.assertTrue(shopping[0]["unmatched"])
        self.assertEqual(total, 0)

    def test_water_is_never_bought(self) -> None:
        aggregate = aggregate_ingredients(
            [
                {"name": "вода", "quantity": Decimal("2"), "unit_code": "l"},
                {"name": "вода фильтрованная", "quantity": Decimal("500"), "unit_code": "ml"},
                {"name": "мука", "quantity": Decimal("200"), "unit_code": "g"},
            ],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        shopping, _, _ = build_shopping(
            aggregate, [], _NoMatcher(), "balanced", _base_quantity
        )
        names = [item["normalized_name"] for item in shopping]
        self.assertEqual(names, ["мука"])

    def test_to_taste_only_bought_when_absent(self) -> None:
        aggregate = aggregate_ingredients(
            [{"name": "соль", "quantity": None, "unit_code": None, "is_to_taste": True}],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        no_stock, _, _ = build_shopping(
            dict(aggregate), [], _NoMatcher(), "balanced", _base_quantity
        )
        self.assertEqual(len(no_stock), 1)
        self.assertIsNone(no_stock[0]["buy_quantity"])

        lots = prepare_inventory(
            [{"name": "Соль", "quantity": Decimal("500"), "unit_code": "g", "expires_on": None}],
            SYNONYMS,
            _normal,
            _base_quantity,
        )
        with_stock, _, _ = build_shopping(
            dict(aggregate), lots, _NoMatcher(), "balanced", _base_quantity
        )
        self.assertEqual(with_stock, [])


if __name__ == "__main__":
    unittest.main()
