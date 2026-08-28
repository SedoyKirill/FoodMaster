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


def plan_for(recipes, rules, synonyms=SYNONYM_ROWS, appliances=(), people=None):
    return build_plan(
        household_id="household",
        starts_on=date(2026, 8, 17),
        days=1,
        cuisines=[],
        people=people or [
            {"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}
        ],
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


class PersonalRulePlanTests(unittest.TestCase):
    """Личные правила и порции по едокам слота (TZ-M8 §3.1–3.2)."""

    CHILD_ID = "11111111-1111-1111-1111-111111111111"
    ADULT_ID = "22222222-2222-2222-2222-222222222222"

    @staticmethod
    def _people(child_eats=("breakfast", "lunch", "dinner")):
        return [
            {
                "id": PersonalRulePlanTests.ADULT_ID, "name": "Взрослый",
                "person_type": "adult", "portion_factor": Decimal("1"),
                "eats_meals": ["breakfast", "lunch", "dinner"],
            },
            {
                "id": PersonalRulePlanTests.CHILD_ID, "name": "Ребёнок",
                "person_type": "child", "portion_factor": Decimal("0.5"),
                "eats_meals": list(child_eats),
            },
        ]

    @staticmethod
    def _recipes():
        recipes = []
        for index, meal in ((1, "breakfast"), (2, "lunch"), (3, "dinner")):
            recipes.append(make_recipe(index, f"Ореховое блюдо {index}", meal, ["орехи", "мука"]))
            recipes.append(make_recipe(index + 10, f"Картофельное блюдо {index}", meal, ["картофель"]))
        return recipes

    def _titles(self, plan):
        return {meal["meal_type"]: meal["title"] for meal in plan["meals"]}

    def test_child_allergy_blocks_only_meals_the_child_eats(self) -> None:
        """Ребёнок обедает в саду — на обед взрослому орехи разрешены.

        В обеденном слоте оставлено единственное блюдо, и оно с орехами:
        так видно, что правило ребёнка на этот слот не действует, а не что
        солвер выбрал другое по цене.
        """
        recipes = [r for r in self._recipes() if r["id"] != 12]
        rules = [{
            "rule_type": "allergy", "term": "орехи", "is_hard": True,
            "person_id": self.CHILD_ID,
        }]
        plan = plan_for(
            recipes, rules, people=self._people(child_eats=("breakfast", "dinner")),
        )
        titles = self._titles(plan)
        self.assertEqual(titles["breakfast"], "Картофельное блюдо 1")
        self.assertEqual(titles["dinner"], "Картофельное блюдо 3")
        self.assertEqual(titles["lunch"], "Ореховое блюдо 2")

    def test_child_allergy_empties_the_slot_the_child_eats(self) -> None:
        """Тот же слот с тем же единственным блюдом, но ребёнок обедает дома."""
        recipes = [r for r in self._recipes() if r["id"] != 12]
        rules = [{
            "rule_type": "allergy", "term": "орехи", "is_hard": True,
            "person_id": self.CHILD_ID,
        }]
        plan = plan_for(recipes, rules, people=self._people())
        self.assertNotIn("lunch", self._titles(plan))
        self.assertTrue(
            any("not_enough_recipes" in warning for warning in plan["warnings"]),
            plan["warnings"],
        )

    def test_family_rule_blocks_every_meal(self) -> None:
        rules = [{"rule_type": "allergy", "term": "орехи", "is_hard": True, "person_id": None}]
        plan = plan_for(self._recipes(), rules, people=self._people())
        self.assertTrue(
            all("Картофельное" in title for title in self._titles(plan).values()),
            self._titles(plan),
        )

    def test_slot_servings_count_only_people_at_home(self) -> None:
        """Обед без ребёнка — одна порция, ужин с ним — полторы."""
        plan = plan_for(
            self._recipes(), [], people=self._people(child_eats=("breakfast", "dinner")),
        )
        servings = {meal["meal_type"]: meal["servings"] for meal in plan["meals"]}
        self.assertEqual(servings["lunch"], Decimal("1"))
        self.assertEqual(servings["dinner"], Decimal("1.5"))

    def test_meal_nobody_eats_at_home_is_not_planned(self) -> None:
        people = [{
            "id": self.ADULT_ID, "name": "Взрослый", "person_type": "adult",
            "portion_factor": Decimal("1"), "eats_meals": ["breakfast", "dinner"],
        }]
        plan = plan_for(self._recipes(), [], people=people)
        self.assertEqual(
            sorted(meal["meal_type"] for meal in plan["meals"]), ["breakfast", "dinner"]
        )

    def test_diet_tag_rule_keeps_only_tagged_recipes(self) -> None:
        recipes = self._recipes()
        for recipe in recipes:
            recipe["diet_tags"] = ["vegetarian"] if "Картофельное" in recipe["title"] else []
        rules = [{
            "rule_type": "exclude", "term": "мясо", "is_hard": True,
            "person_id": self.ADULT_ID, "diet_tag": "vegetarian",
        }]
        plan = plan_for(recipes, rules, people=self._people())
        self.assertTrue(all("Картофельное" in title for title in self._titles(plan).values()))
        self.assertFalse(any("diet_conflict" in w for w in plan["warnings"]))

    def test_missing_diet_recipes_warn_instead_of_empty_plan(self) -> None:
        recipes = self._recipes()
        for recipe in recipes:
            recipe["diet_tags"] = []
        rules = [{
            "rule_type": "exclude", "term": "мясо", "is_hard": True,
            "person_id": self.ADULT_ID, "diet_tag": "vegetarian",
        }]
        plan = plan_for(recipes, rules, people=self._people())
        self.assertEqual(len(plan["meals"]), 3)
        self.assertTrue(any("diet_conflict" in w for w in plan["warnings"]), plan["warnings"])


if __name__ == "__main__":
    unittest.main()
