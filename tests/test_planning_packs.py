"""Целые упаковки на весь горизонт (TZ-M8 §6.3)."""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import (  # noqa: E402
    ProductMatcher, _base_quantity, _normal, build_plan,
)
from app.web.planning.candidates import Synonyms, _stock_lots  # noqa: E402
from app.web.planning.packs import (  # noqa: E402
    DEFAULT_PERISH, build_pack_model, perish_of,
)

SYNONYMS = Synonyms.from_rows([])

PRODUCTS = [
    {
        "id": 1,
        "name": "Сметана 20%",
        "pack_quantity": 300,
        "pack_unit": "g",
        "effective_price_kop": 12000,
        "category_slugs": ["molochnye-produkty-yajjco-3"],
    },
    {
        "id": 2,
        "name": "Крупа гречневая ядрица",
        "pack_quantity": 800,
        "pack_unit": "g",
        "effective_price_kop": 9000,
        "category_slugs": ["makarony-krupy-muka-25"],
    },
]


def ingredient(name: str, grams: str) -> dict:
    return {
        "ingredient_text": name,
        "normalized_name": name,
        "quantity_min": Decimal(grams),
        "quantity_max": Decimal(grams),
        "unit_code": "g",
    }


def recipe(recipe_id: int, title: str, meal: str, ingredients: list[dict]) -> dict:
    return {
        "id": recipe_id,
        "title": title,
        "source_page_start": recipe_id,
        "source_servings_min": Decimal("1"),
        "cuisine_code": "russian",
        "meal_types": [meal],
        "dish_type": "stew",
        "appliances": [],
        "review_status": "ready",
        "extraction_confidence": Decimal("0.9"),
        "source_title": "Книга",
        "ingredients": ingredients,
    }


class PerishTests(unittest.TestCase):
    def test_dairy_spoils_and_cereal_does_not(self) -> None:
        self.assertEqual(perish_of(PRODUCTS[0]), 1.0)
        self.assertEqual(perish_of(PRODUCTS[1]), 0.0)

    def test_unknown_category_sits_in_the_middle(self) -> None:
        self.assertEqual(perish_of({"category_slugs": ["чего-то-новое"]}), DEFAULT_PERISH)

    def test_worst_category_wins(self) -> None:
        product = {"category_slugs": ["makarony-krupy-muka-25", "molochnye-produkty-yajjco-3"]}
        self.assertEqual(perish_of(product), 1.0)


class PackModelTests(unittest.TestCase):
    """Общий товар моделируется, одиночный остаётся в цене блюда."""

    def setUp(self) -> None:
        self.matcher = ProductMatcher(PRODUCTS)
        self.recipes = [
            recipe(1, "Рагу со сметаной", "dinner", [ingredient("сметана", "200")]),
            recipe(2, "Запеканка со сметаной", "lunch", [ingredient("сметана", "200")]),
            recipe(3, "Гречка", "dinner", [ingredient("гречка", "300")]),
        ]

    def _model(self, costs=None, **kwargs):
        return build_pack_model(
            recipe_ids=[1, 2, 3],
            recipes_by_id={int(item["id"]): item for item in self.recipes},
            costs_by_recipe=costs or {1: 8000, 2: 8000, 3: 3375},
            slots=6,
            scale_of=lambda _recipe: Decimal("1"),
            matcher=self.matcher,
            price_tier="balanced",
            stock=[],
            synonyms=SYNONYMS,
            normal=_normal,
            base_quantity=_base_quantity,
            **kwargs,
        )

    def test_only_products_shared_by_two_dishes_enter_the_model(self) -> None:
        model = self._model()
        self.assertEqual(sorted(model.products), [1])
        self.assertEqual(model.need(1, 1), 200)
        self.assertEqual(model.need(2, 1), 200)
        self.assertEqual(model.need(3, 2), 0)

    def test_shared_product_leaves_the_dish_price_and_single_use_is_a_whole_pack(self) -> None:
        """Сметану считает модель, а пачка гречки покупается целиком.

        Скоринг оценивал гречку по пропорции — 300 г от пачки 800 г за 90 ₽,
        то есть 33.75 ₽. Но в магазине берут пачку: блюдо стоит 90 ₽ (§6.3).
        """
        model = self._model()
        self.assertEqual(model.private_cost_kop[1], 0)
        self.assertEqual(model.private_cost_kop[3], 9000)

    def test_pack_size_and_perish_come_from_the_catalogue(self) -> None:
        product = self._model().products[1]
        self.assertEqual(product.pack_base, 300)
        self.assertEqual(product.price_kop, 12000)
        self.assertEqual(product.perish, 1.0)

    def test_stock_at_home_is_known_to_the_model(self) -> None:
        inventory = [{"name": "сметана", "quantity": Decimal("250"), "unit_code": "g"}]
        model = build_pack_model(
            recipe_ids=[1, 2, 3],
            recipes_by_id={int(item["id"]): item for item in self.recipes},
            costs_by_recipe={1: 8000, 2: 8000, 3: 3375},
            slots=6,
            scale_of=lambda _recipe: Decimal("1"),
            matcher=self.matcher,
            price_tier="balanced",
            stock=_stock_lots(inventory, SYNONYMS, _normal, _base_quantity),
            synonyms=SYNONYMS,
            normal=_normal,
            base_quantity=_base_quantity,
        )
        self.assertEqual(model.products[1].stock_base, 250)

    def test_model_is_truncated_and_says_so(self) -> None:
        model = self._model(max_products=0)
        self.assertTrue(model.truncated)
        self.assertEqual(model.products, {})
        # Ничего не смоделировано — сметана стала одиночным товаром и
        # считается целой пачкой, как и всё остальное вне модели.
        self.assertEqual(model.private_cost_kop[1], 12000)


class PackPlanTests(unittest.TestCase):
    """Приёмка §9.1.6: два блюда на сметане — одна пачка в списке."""

    def _plan(self) -> dict:
        recipes = [
            recipe(1, "Рагу со сметаной", "dinner", [ingredient("сметана", "100")]),
            recipe(2, "Запеканка со сметаной", "lunch", [ingredient("сметана", "100")]),
        ]
        return build_plan(
            household_id="household",
            starts_on=date(2026, 8, 31),
            days=1,
            cuisines=[],
            people=[
                {"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}
            ],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=recipes,
            products=PRODUCTS,
            meals=["lunch", "dinner"],
        )

    def test_one_pack_of_sour_cream_covers_both_dishes(self) -> None:
        plan = self._plan()
        sour_cream = [
            item for item in plan["shopping"] if item["normalized_name"] == "сметана"
        ]
        self.assertEqual(len(sour_cream), 1)
        self.assertEqual(sour_cream[0]["quantity"], Decimal("200"))
        self.assertEqual(sour_cream[0]["pack_count"], 1)

    def test_both_dishes_say_they_share_the_pack(self) -> None:
        """Причина §5: «одна пачка сметаны с ужином»."""
        plan = self._plan()
        for meal in plan["meals"]:
            reason = next(
                (item for item in meal["reasons"] if item["code"] == "shares_pack"), None
            )
            self.assertIsNotNone(reason, meal["title"])
            self.assertEqual(reason["ingredient"], "сметана")
            self.assertNotEqual(reason["other_meal"], 0)

    def test_sharing_a_pack_beats_a_cheaper_looking_dish(self) -> None:
        """Ради чего всё это: блюдо на уже купленной пачке дешевле нового товара.

        По пропорции пачки гречка выглядит дешевле (33.75 ₽ против 80 ₽), и
        до M8 солвер выбирал её. На самом деле пачку гречки покупают целиком
        за 90 ₽, а сметана на запеканку уже куплена ради ужина.
        """
        recipes = [
            recipe(1, "Рагу со сметаной", "dinner", [ingredient("сметана", "100")]),
            recipe(2, "Запеканка со сметаной", "lunch", [ingredient("сметана", "100")]),
            recipe(3, "Гречка", "lunch", [ingredient("гречка", "300")]),
        ]
        plan = build_plan(
            household_id="household",
            starts_on=date(2026, 8, 31),
            days=1,
            cuisines=[],
            people=[
                {"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}
            ],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=recipes,
            products=PRODUCTS,
            meals=["lunch", "dinner"],
        )
        lunch = next(meal for meal in plan["meals"] if meal["meal_type"] == "lunch")
        self.assertEqual(lunch["title"], "Запеканка со сметаной")
        # Одна пачка сметаны на оба блюда — 120 ₽ вместо 120 + 90 ₽.
        self.assertEqual(plan["estimated_cost_kop"], 12000)

    def test_plan_cost_equals_the_shopping_list(self) -> None:
        """Расхождение «модель считает книжную цену, список — пачки» ушло."""
        plan = self._plan()
        self.assertEqual(
            plan["estimated_cost_kop"],
            sum(item["estimated_cost_kop"] or 0 for item in plan["shopping"]),
        )
        self.assertEqual(plan["estimated_cost_kop"], 12000)


if __name__ == "__main__":
    unittest.main()
