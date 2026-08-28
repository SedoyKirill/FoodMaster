"""TZ-M5R §4, тесты 1–4: ограничения через словарь синонимов."""

import os
import sys
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import _normal, build_plan
from app.web.planning.candidates import (
    Synonyms, hard_rule_terms, ingredient_matches_terms, soft_rule_terms,
)

SYNONYM_ROWS = [
    {"term": "орехи", "canonical": "орех", "kind": "form"},
    {"term": "фундук", "canonical": "орех", "kind": "group"},
    {"term": "миндаль", "canonical": "орех", "kind": "group"},
    {"term": "мука", "canonical": "глютен", "kind": "group"},
    {"term": "пшеница", "canonical": "глютен", "kind": "group"},
]


def make_recipe(recipe_id, title, meal_type, ingredient_names):
    return {
        "id": recipe_id,
        "title": title,
        "source_page_start": recipe_id,
        "source_servings_min": Decimal("2"),
        "cuisine_code": "russian",
        "meal_types": [meal_type],
        "appliances": [],
        "review_status": "needs_review",
        "extraction_confidence": Decimal("0.9"),
        "ingredients": [
            {
                "ingredient_text": name,
                "normalized_name": name,
                "quantity_min": Decimal("100"),
                "quantity_max": Decimal("100"),
                "unit_code": "g",
            }
            for name in ingredient_names
        ],
    }


def plan_for(recipes, rules, synonyms=SYNONYM_ROWS, appliances=()):
    return build_plan(
        household_id="household",
        starts_on=date(2026, 8, 17),
        days=1,
        cuisines=[],
        people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
        appliances=list(appliances),
        rules=rules,
        inventory=[],
        recipes=recipes,
        products=[],
        synonyms=synonyms,
    )


class SynonymRuleTests(unittest.TestCase):
    def test_allergy_matches_through_group_synonym(self) -> None:
        """Тест 1: правило «орехи» исключает рецепт с «фундук»."""
        synonyms = Synonyms.from_rows(SYNONYM_ROWS)
        banned = hard_rule_terms(
            [{"rule_type": "allergy", "term": "орехи", "is_hard": True}], synonyms, _normal
        )
        self.assertIn("орех", banned)
        self.assertTrue(ingredient_matches_terms("фундук", banned, synonyms, _normal))
        self.assertTrue(ingredient_matches_terms("миндаль жареный", banned, synonyms, _normal))

    def test_oats_not_excluded_by_gluten_without_synonym(self) -> None:
        """Тест 2: «овсянка» НЕ исключается правилом «глютен» без явного синонима."""
        synonyms = Synonyms.from_rows(SYNONYM_ROWS)
        banned = hard_rule_terms(
            [{"rule_type": "intolerance", "term": "глютен", "is_hard": True}], synonyms, _normal
        )
        self.assertFalse(ingredient_matches_terms("овсянка", banned, synonyms, _normal))
        self.assertFalse(ingredient_matches_terms("овсяные хлопья", banned, synonyms, _normal))
        self.assertTrue(ingredient_matches_terms("мука пшеничная", banned, synonyms, _normal))

    def test_allergy_excludes_recipe_from_plan(self) -> None:
        recipes = [
            make_recipe(1, "Каша с фундуком", "breakfast", ["крупа", "фундук"]),
            make_recipe(2, "Каша простая", "breakfast", ["крупа", "молоко"]),
            make_recipe(3, "Суп овощной", "lunch", ["картофель", "морковь"]),
            make_recipe(4, "Рагу", "dinner", ["кабачок", "томат"]),
        ]
        plan = plan_for(
            recipes, [{"rule_type": "allergy", "term": "орехи", "is_hard": True}]
        )
        chosen = {meal["recipe_id"] for meal in plan["meals"]}
        self.assertNotIn(1, chosen)
        self.assertIn(2, chosen)

    def test_dislike_is_soft_but_loses_to_equal(self) -> None:
        """Тест 3: dislike не выбрасывает рецепт, но проигрывает равному."""
        recipes = [
            make_recipe(1, "Каша с луком", "breakfast", ["крупа", "лук"]),
            make_recipe(2, "Каша нейтральная", "breakfast", ["крупа", "молоко"]),
            make_recipe(3, "Суп", "lunch", ["картофель"]),
            make_recipe(4, "Рагу", "dinner", ["кабачок"]),
        ]
        rules = [{"rule_type": "dislike", "term": "лук", "is_hard": False}]
        plan = plan_for(recipes, rules)
        breakfast = next(m for m in plan["meals"] if m["meal_type"] == "breakfast")
        self.assertEqual(breakfast["recipe_id"], 2)

        # Если нейтральной альтернативы нет, dislike-рецепт всё же попадает в план.
        plan_without_alternative = plan_for(
            [recipes[0], recipes[2], recipes[3]], rules
        )
        breakfast = next(
            m for m in plan_without_alternative["meals"] if m["meal_type"] == "breakfast"
        )
        self.assertEqual(breakfast["recipe_id"], 1)

    def test_soft_terms_do_not_include_hard_rules(self) -> None:
        synonyms = Synonyms.from_rows(SYNONYM_ROWS)
        soft = soft_rule_terms(
            [
                {"rule_type": "allergy", "term": "орехи", "is_hard": True},
                {"rule_type": "dislike", "term": "лук", "is_hard": False},
            ],
            synonyms,
            _normal,
        )
        self.assertEqual(soft, {"лук"})

    def test_appliances_filter_even_when_household_list_is_empty(self) -> None:
        """Тест 4 (TZ-M8 T1, P8): техника фильтрует всегда.

        Раньше пустой список означал «фильтр выключен», и семье без гриля
        планировались блюда для гриля. Теперь набор техники семье выдаётся
        при регистрации, а пустой список честно ничего не разрешает.
        """
        recipes = [
            make_recipe(1, "Каша", "breakfast", ["крупа"]),
            make_recipe(2, "Суп", "lunch", ["картофель"]),
            make_recipe(3, "Рагу", "dinner", ["кабачок"]),
        ]
        for recipe in recipes:
            recipe["appliances"] = ["oven", "blender"]
        with self.assertRaises(ValueError):
            plan_for(recipes, [], appliances=())
        plan = plan_for(recipes, [], appliances=("oven", "blender"))
        self.assertEqual(len(plan["meals"]), 3)


if __name__ == "__main__":
    unittest.main()
