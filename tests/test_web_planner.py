import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import (
    DEFAULT_APPLIANCES,
    ProductMatcher,
    ProductMatcherCache,
    _food_token_key,
    _ingredient_cost_hint,
    _normal,
    _product_match,
    _tokens,
    build_plan,
    slot_alternatives,
    clean_dish_title,
    is_dish_title,
    is_recipe_clean,
    warm_product_matcher,
)
from fakes import StubClock  # noqa: E402


def recipe(recipe_id: int, title: str, meal_type: str, ingredient: str = "Молоко") -> dict:
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
        "source_title": "Тестовая книга",
        "ingredients": [
            {
                "ingredient_text": ingredient,
                "normalized_name": ingredient.lower(),
                "quantity_min": Decimal("200"),
                "quantity_max": Decimal("200"),
                "unit_code": "ml" if ingredient == "Молоко" else "g",
            }
        ],
    }


class PlannerTests(unittest.TestCase):
    def test_raw_product_matching_uses_full_name_not_only_first_word(self) -> None:
        products = [
            {
                "id": 1,
                "name": "Крупа гречневая ядрица",
                "pack_quantity": 800,
                "pack_unit": "g",
                "effective_price_kop": 9000,
                "category_slugs": ["makarony-krupy-muka-25"],
            },
            {
                "id": 2,
                "name": "Каша с гречкой и грибами",
                "pack_quantity": 250,
                "pack_unit": "g",
                "effective_price_kop": 7000,
                "category_slugs": ["gotovaya-eda-42"],
            },
        ]
        match = _product_match("гречки", "g", products, "balanced")
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], 1)
        self.assertEqual(ProductMatcher(products).match("гречки", "g", "balanced")["id"], 1)
        false_match = _product_match(
            "шпината",
            "g",
            [
                {
                    "id": 3,
                    "name": "Колбаски для гриля со шпинатом",
                    "pack_quantity": 180,
                    "pack_unit": "g",
                    "effective_price_kop": 17000,
                    "category_slugs": ["kolbasa-sosiski-754"],
                }
            ],
            "balanced",
        )
        self.assertIsNone(false_match)

    def test_non_dish_pdf_headings_are_rejected(self) -> None:
        self.assertFalse(is_dish_title("ЗАВТРАКИ Рецепты на каждый день"))
        self.assertFalse(is_dish_title("ПРИГОТОВЛЕНИЕ МЯСА И РЫБЫ"))
        self.assertFalse(is_dish_title("ПРИГОТОВЛЕHИЕ МЯСА И РЫБЫ"))
        self.assertFalse(is_dish_title("HOT BUTTERED PEAS WITH BACON AND"))
        self.assertFalse(is_dish_title("Сахар кокосовый или"))
        self.assertFalse(is_dish_title("SERVES 4 TO 6"))
        self.assertFalse(is_dish_title("НА ЗАМЕТКУ. Можно приготовить заранее"))
        self.assertFalse(is_dish_title("Кто не любит запеченный картофель?"))
        self.assertFalse(is_dish_title("НА 4 ПОРЦИИ"))
        self.assertFalse(is_dish_title("Immediately remove from the heat"))
        self.assertFalse(is_dish_title("Правильно – любимый человек рядом"))
        self.assertFalse(is_dish_title("ЗАВТРАКИ Для гречневого блина"))
        self.assertFalse(is_dish_title("МЯСО И РЫБА Курица"))
        self.assertFalse(is_dish_title("Салаты и закуски РЕЦЕПТ"))
        self.assertFalse(is_dish_title("BEEF AND BARLEY STEW"))
        self.assertFalse(is_dish_title("Курица BBQ"))
        self.assertFalse(is_dish_title("Сырные вафли на"))
        self.assertFalse(is_dish_title("семечки равномерно не распределятся по всему"))
        self.assertFalse(is_dish_title("НА 12 ФОРМОЧЕК"))
        self.assertFalse(
            is_dish_title(
                "Натто на завтрак Натто — очень питательный ферментированный продукт "
                "из соевых бобов, который любят многие"
            )
        )
        self.assertTrue(is_dish_title("Жареный рис с креветками по-корейски"))

    def test_english_recipe_content_is_not_user_visible(self) -> None:
        self.assertFalse(
            is_recipe_clean(
                {
                    "title": "Овощной суп",
                    "ingredients": [{"normalized_name": "картофель"}],
                    "steps": [{"instruction": "Cook until tender"}],
                }
            )
        )

    def test_section_prefix_is_removed_from_dish_title(self) -> None:
        self.assertEqual(
            clean_dish_title("Еда как праздник НОЧНАЯ ОВСЯНКА"),
            "НОЧНАЯ ОВСЯНКА",
        )
        self.assertEqual(
            clean_dish_title("КОПЧЕНЫЕ СВИНЫЕ РЕБРЫШКИ Для душевной компании"),
            "КОПЧЕНЫЕ СВИНЫЕ РЕБРЫШКИ",
        )
        self.assertFalse(
            is_recipe_clean(
                {
                    "title": "Овощной суп",
                    "ingredients": [{"normalized_name": "chicken stock"}],
                    "steps": [{"instruction": "Варить до готовности"}],
                }
            )
        )

    def test_three_day_plan_scales_and_uses_inventory(self) -> None:
        recipes = []
        for index in range(1, 4):
            recipes.append(recipe(index, f"Каша {index}", "breakfast"))
            recipes.append(recipe(index + 10, f"Суп {index}", "lunch"))
            recipes.append(recipe(index + 20, f"Рыба {index}", "dinner"))
        recipes.append(recipe(99, "Грибы со сливками", "dinner", "Грибы"))
        recipes.append(recipe(100, "ЗАВТРАКИ Рецепты на каждый день", "breakfast"))
        recipes.append(
            recipe(
                101,
                "Салат с молодой капустой",
                "lunch",
                "Капусту мелко нарезать и хорошо помять руками",
            )
        )
        recipes[0]["ingredients"].append(
            {
                "ingredient_text": "Соль",
                "normalized_name": "соль",
                "quantity_min": None,
                "quantity_max": None,
                "unit_code": None,
            }
        )
        plan = build_plan(
            household_id="household",
            starts_on=date(2026, 8, 15),
            days=3,
            cuisines=["russian"],
            people=[
                {"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")},
                {"name": "Ребёнок", "person_type": "child", "portion_factor": Decimal("0.5")},
            ],
            appliances=[],
            rules=[{"term": "грибы", "is_hard": True}],
            inventory=[
                {"name": "Молоко", "quantity": Decimal("500"), "unit_code": "ml", "expires_on": date(2026, 8, 16)}
            ],
            recipes=recipes,
            products=[
                {
                    "id": 1,
                    "name": "Молоко пастеризованное",
                    "pack_quantity": Decimal("1000"),
                    "pack_unit": "ml",
                    "effective_price_kop": 9999,
                }
            ],
        )
        self.assertEqual(len(plan["meals"]), 9)
        self.assertNotIn(99, {meal["recipe_id"] for meal in plan["meals"]})
        self.assertNotIn(100, {meal["recipe_id"] for meal in plan["meals"]})
        self.assertNotIn(101, {meal["recipe_id"] for meal in plan["meals"]})
        self.assertTrue(all(meal["servings"] == Decimal("1.5") for meal in plan["meals"]))
        milk = next(item for item in plan["shopping"] if item["normalized_name"] == "молоко")
        self.assertEqual(milk["covered_from_inventory"], Decimal("500"))
        self.assertGreater(milk["buy_quantity"], 0)
        self.assertGreater(plan["estimated_cost_kop"], 0)
        self.assertTrue(any(item["normalized_name"] == "соль" for item in plan["shopping"]))

    def test_price_tier_changes_selected_product_cost(self) -> None:
        recipes = []
        for index in range(1, 4):
            recipes.append(recipe(index, f"Каша {index}", "breakfast"))
            recipes.append(recipe(index + 10, f"Суп {index}", "lunch"))
            recipes.append(recipe(index + 20, f"Запеканка {index}", "dinner"))
        products = [
            {"id": 1, "name": "Молоко эконом", "pack_quantity": 1000, "pack_unit": "ml", "effective_price_kop": 8000},
            {"id": 2, "name": "Молоко премиум", "pack_quantity": 1000, "pack_unit": "ml", "effective_price_kop": 18000},
        ]
        common = dict(
            household_id="household",
            starts_on=date(2026, 8, 15),
            days=3,
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": 1}],
            appliances=[], rules=[], inventory=[], recipes=recipes, products=products,
        )
        economy = build_plan(**common, price_tier="economy")
        premium = build_plan(**common, price_tier="premium")
        self.assertLess(economy["estimated_cost_kop"], premium["estimated_cost_kop"])
        self.assertEqual(economy["price_tier"], "economy")
        self.assertEqual(premium["price_tier"], "premium")

    def test_declined_food_names_match_quality_products(self) -> None:
        butter_products = [
            {
                "id": 1,
                "name": "Спред сливочно-растительный 72%",
                "pack_quantity": 180,
                "pack_unit": "g",
                "effective_price_kop": 6999,
            },
            {
                "id": 2,
                "name": "Масло сливочное Крестьянское 72,5%, без змж",
                "pack_quantity": 180,
                "pack_unit": "g",
                "effective_price_kop": 13999,
            },
            {
                "id": 3,
                "name": "Масло сливочное Традиционное несоленое 82,5% ГОСТ, без змж",
                "pack_quantity": 180,
                "pack_unit": "g",
                "effective_price_kop": 19999,
            },
            {
                "id": 4,
                "name": "Масло сливочное Шоколадное 82,5%, без змж",
                "pack_quantity": 180,
                "pack_unit": "g",
                "effective_price_kop": 9999,
            },
        ]
        butter = _product_match("сливочного масла", "g", butter_products, "economy")
        self.assertIsNotNone(butter)
        self.assertEqual(butter["id"], 3)

        cheese = _product_match(
            "сливочного сыра",
            "g",
            [
                {
                    "id": 9,
                    "name": "Чипсы картофельные со вкусом сливочного сыра",
                    "pack_quantity": 240,
                    "pack_unit": "g",
                    "effective_price_kop": 4999,
                },
                {
                    "id": 10,
                    "name": "Сырный продукт сливочный с заменителем молочного жира",
                    "pack_quantity": 200,
                    "pack_unit": "g",
                    "effective_price_kop": 8999,
                },
                {
                    "id": 12,
                    "name": "Сыр плавленый Сливочный 55%, без змж",
                    "pack_quantity": 200,
                    "pack_unit": "g",
                    "effective_price_kop": 7999,
                },
                {
                    "id": 11,
                    "name": "Сыр творожный сливочный, без змж",
                    "pack_quantity": 200,
                    "pack_unit": "g",
                    "effective_price_kop": 15999,
                },
            ],
            "balanced",
        )
        self.assertIsNotNone(cheese)
        self.assertEqual(cheese["id"], 11)

    def test_generic_flour_means_wheat_flour(self) -> None:
        products = [
            {
                "id": 1,
                "name": "Мука рисовая",
                "pack_quantity": 500,
                "pack_unit": "g",
                "effective_price_kop": 5999,
            },
            {
                "id": 2,
                "name": "Мука пшеничная хлебопекарная высший сорт ГОСТ",
                "pack_quantity": 2000,
                "pack_unit": "g",
                "effective_price_kop": 11999,
            },
        ]
        flour = ProductMatcher(products).match("муки", "g", "economy")
        self.assertIsNotNone(flour)
        self.assertEqual(flour["id"], 2)

    def test_cooking_oil_does_not_match_specialty_blend(self) -> None:
        products = [
            {
                "id": 1,
                "name": "Масло растительное смесь льняного и тыквенного нерафинированное",
                "pack_quantity": 250,
                "pack_unit": "ml",
                "effective_price_kop": 9999,
            },
            {
                "id": 2,
                "name": "Масло подсолнечное рафинированное дезодорированное высший сорт",
                "pack_quantity": 800,
                "pack_unit": "ml",
                "effective_price_kop": 10499,
            },
            {
                "id": 5,
                "name": "Масло-спрей подсолнечное рафинированное дезодорированное",
                "pack_quantity": 250,
                "pack_unit": "ml",
                "effective_price_kop": 4999,
            },
            {
                "id": 3,
                "name": "Масло растительное смесь льняного и кунжутного нерафинированное",
                "pack_quantity": 250,
                "pack_unit": "ml",
                "effective_price_kop": 14499,
            },
            {
                "id": 4,
                "name": "Масло кунжутное нерафинированное",
                "pack_quantity": 250,
                "pack_unit": "ml",
                "effective_price_kop": 25499,
            },
        ]
        matcher = ProductMatcher(products)
        self.assertEqual(
            matcher.match("растительного масла", "ml", "balanced", Decimal("15"))["id"],
            2,
        )
        self.assertEqual(
            matcher.match("кунжутного масла", "ml", "balanced", Decimal("15"))["id"],
            4,
        )

    def test_required_quantity_avoids_oversized_package(self) -> None:
        products = [
            {
                "id": 1,
                "name": "Молоко маленькая упаковка",
                "pack_quantity": 200,
                "pack_unit": "ml",
                "effective_price_kop": 5000,
            },
            {
                "id": 2,
                "name": "Молоко большая упаковка",
                "pack_quantity": 1000,
                "pack_unit": "ml",
                "effective_price_kop": 10000,
            },
        ]
        self.assertEqual(
            _product_match(
                "молоко", "ml", products, "economy", Decimal("150")
            )["id"],
            1,
        )
        self.assertEqual(
            _product_match(
                "молоко", "ml", products, "premium", Decimal("150")
            )["id"],
            1,
        )


class ProductMatcherCacheTests(unittest.TestCase):
    """B4/A5 — каталог перечитывается не чаще раза в TTL и только при смене отметки."""

    def setUp(self) -> None:
        self.clock = StubClock()
        self.cache = ProductMatcherCache(ttl_seconds=600.0, clock=self.clock)
        self.stamp_calls = 0
        self.product_calls = 0
        self.stamp = "2026-08-17"
        self.products = [
            {"id": 1, "name": "Молоко", "pack_quantity": 1000, "pack_unit": "ml",
             "effective_price_kop": 9900}
        ]

    async def _load_stamp(self):
        self.stamp_calls += 1
        return self.stamp

    async def _load_products(self):
        self.product_calls += 1
        return self.products

    def _get(self):
        return asyncio.run(self.cache.get(self._load_stamp, self._load_products))

    def test_b4_a5_matcher_cache_reuses_instance_within_ttl(self) -> None:
        first = self._get()
        second = self._get()
        self.assertIs(first, second)
        self.assertEqual(self.cache.loads, 1)
        self.assertEqual(self.stamp_calls, 1, "внутри TTL отметка не должна запрашиваться")
        self.assertEqual(self.product_calls, 1)

    def test_b4_a5_matcher_cache_checks_stamp_after_ttl_without_reloading(self) -> None:
        first = self._get()
        self.clock.advance(601)
        second = self._get()
        self.assertIs(first, second, "кэш должен пережить проверку неизменной отметки")
        self.assertEqual(self.stamp_calls, 2)
        self.assertEqual(self.product_calls, 1)
        self.assertEqual(self.cache.loads, 1)

    def test_b4_a5_matcher_cache_rebuilds_when_stamp_changes(self) -> None:
        first = self._get()
        self.clock.advance(601)
        self.stamp = "2026-08-18"
        second = self._get()
        self.assertIsNot(first, second)
        self.assertEqual(self.cache.loads, 2)

    def test_b4_a5_matcher_cache_is_concurrency_safe(self) -> None:
        async def race():
            return await asyncio.gather(
                self.cache.get(self._load_stamp, self._load_products),
                self.cache.get(self._load_stamp, self._load_products),
                self.cache.get(self._load_stamp, self._load_products),
            )

        matchers = asyncio.run(race())
        self.assertEqual(len({id(matcher) for matcher in matchers}), 1)
        self.assertEqual(self.product_calls, 1)

    def test_b4_a5_invalidate_forces_reload(self) -> None:
        self._get()
        self.cache.invalidate()
        self._get()
        self.assertEqual(self.cache.loads, 2)

    def test_b4_a5_build_plan_accepts_external_matcher(self) -> None:
        matcher = ProductMatcher(self.products)
        recipes = []
        for index in range(1, 4):
            recipes.append(recipe(index, f"Каша {index}", "breakfast"))
            recipes.append(recipe(index + 10, f"Суп {index}", "lunch"))
            recipes.append(recipe(index + 20, f"Рагу {index}", "dinner"))
        plan = build_plan(
            household_id="household", starts_on=date(2026, 8, 17), days=3, cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[], rules=[], inventory=[], recipes=recipes,
            products=self.products, product_matcher=matcher,
        )
        self.assertEqual(len(plan["meals"]), 9)
        self.assertGreater(plan["estimated_cost_kop"], 0)


class PlanReasonsTests(unittest.TestCase):
    """Каждое блюдо плана объясняет себя (TZ-M8 §5)."""

    @staticmethod
    def _plan(**kwargs) -> dict:
        recipes = [recipe(index, f"Блюдо {index}", "dinner") for index in (1, 2, 3)]
        return build_plan(
            household_id="household",
            starts_on=date(2026, 8, 26),
            days=1,
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=kwargs.pop("inventory", []),
            recipes=recipes,
            products=[],
            meals=["dinner"],
            **kwargs,
        )

    def test_every_meal_carries_at_least_one_reason(self) -> None:
        plan = self._plan()
        for meal in plan["meals"]:
            self.assertTrue(meal["reasons"], meal["title"])
            self.assertLessEqual(len(meal["reasons"]), 3)

    def test_expiring_stock_becomes_the_reason(self) -> None:
        """Молоко портится через два дня — это и есть довод за блюдо."""
        inventory = [{
            "name": "Молоко", "quantity": Decimal("200"), "unit_code": "ml",
            "expires_on": date(2026, 8, 28),
        }]
        plan = self._plan(inventory=inventory)
        codes = [reason["code"] for reason in plan["meals"][0]["reasons"]]
        self.assertIn("uses_expiring", codes)

    def test_rotation_is_named_for_a_long_forgotten_dish(self) -> None:
        history = [{"recipe_id": 1, "meal_date": date(2026, 8, 10), "dish_type": None}]
        plan = self._plan(history=history)
        by_recipe = {meal["recipe_id"]: meal["reasons"] for meal in plan["meals"]}
        if 1 in by_recipe:
            self.assertIn("rotation", [reason["code"] for reason in by_recipe[1]])


class AlternativeGroupTests(unittest.TestCase):
    """Замена предлагает похожее, другое и новое (TZ-M8 §6.6)."""

    @staticmethod
    def _recipes() -> list[dict]:
        recipes = []
        for index in range(1, 5):
            item = recipe(index, f"Суп номер {index}", "dinner")
            item["dish_type"] = "soup"
            recipes.append(item)
        for index in range(5, 9):
            item = recipe(index, f"Запеканка номер {index}", "dinner")
            item["dish_type"] = "casserole"
            recipes.append(item)
        return recipes

    def _cards(self, **kwargs) -> list[dict]:
        return slot_alternatives(
            meal_date=date(2026, 8, 26),
            meal_type="dinner",
            current_recipe_id=1,
            other_meals=[],
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=self._recipes(),
            products=[],
            limit=10,
            with_details=True,
            **kwargs,
        )

    def test_cards_are_split_into_groups(self) -> None:
        """Семья, которая знает все блюда, получает «похожее» и «другое»."""
        history = [
            {"recipe_id": index, "meal_date": date(2026, 8, 20), "dish_type": None}
            for index in range(1, 9)
        ]
        groups = {card["group"] for card in self._cards(history=history)}
        self.assertIn("similar", groups)
        self.assertIn("other", groups)

    def test_new_group_appears_when_the_family_knows_nothing(self) -> None:
        groups = {card["group"] for card in self._cards()}
        self.assertIn("similar", groups)
        self.assertIn("new", groups)

    def test_similar_group_keeps_the_dish_type_of_the_current_meal(self) -> None:
        for card in self._cards():
            if card["group"] == "similar":
                self.assertEqual(card["recipe"]["dish_type"], "soup")

    def test_unknown_dish_of_another_type_lands_in_the_new_group(self) -> None:
        """Знакомы все, кроме одной запеканки, — она и есть «новое»."""
        history = [
            {"recipe_id": index, "meal_date": date(2026, 8, 20), "dish_type": None}
            for index in range(1, 8)
        ]
        cards = self._cards(history=history)
        new_cards = [card for card in cards if card["group"] == "new"]
        self.assertEqual([card["recipe"]["id"] for card in new_cards], [8])

    def test_every_card_has_one_reason_and_deltas(self) -> None:
        for card in self._cards():
            self.assertIn("code", card["reason"])
            self.assertIsNotNone(card["delta_cost_kop"])

    def test_current_dish_is_never_offered(self) -> None:
        self.assertNotIn(1, [card["recipe"]["id"] for card in self._cards()])


class HistoryAndTimeTests(unittest.TestCase):
    """Ротация и время готовки в плане (TZ-M8 §3.5, §3.7)."""

    @staticmethod
    def _recipe(recipe_id: int, title: str, minutes: int | None = None) -> dict:
        item = recipe(recipe_id, title, "dinner")
        item["time_total_minutes"] = minutes
        return item

    @staticmethod
    def _plan(recipes: list[dict], **kwargs) -> dict:
        return build_plan(
            household_id="household",
            starts_on=kwargs.pop("starts_on", date(2026, 8, 26)),  # среда
            days=1,
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=recipes,
            products=[],
            meals=["dinner"],
            **kwargs,
        )

    def test_dish_eaten_yesterday_loses_to_a_forgotten_one(self) -> None:
        recipes = [self._recipe(1, "Гречка с грибами"), self._recipe(2, "Рис с овощами")]
        history = [{"recipe_id": 1, "meal_date": date(2026, 8, 25), "dish_type": None}]
        plan = self._plan(recipes, history=history)
        self.assertEqual(plan["meals"][0]["recipe_id"], 2)

    def test_history_older_than_three_weeks_does_not_matter(self) -> None:
        recipes = [self._recipe(1, "Гречка с грибами"), self._recipe(2, "Рис с овощами")]
        history = [{"recipe_id": 1, "meal_date": date(2026, 7, 1), "dish_type": None}]
        plan = self._plan(recipes, history=history)
        self.assertEqual(plan["meals"][0]["recipe_id"], 1)  # порядок как без истории

    def test_weekday_dinner_avoids_long_recipes(self) -> None:
        recipes = [
            self._recipe(index, f"Долгое блюдо {index}", minutes=120) for index in range(1, 9)
        ] + [self._recipe(9, "Быстрое блюдо", minutes=30)]
        plan = self._plan(recipes, plan_profile={"weekday_max_minutes": 45})
        self.assertEqual(plan["meals"][0]["recipe_id"], 9)
        self.assertNotIn(
            "time_limit_relaxed", " ".join(plan["warnings"])
        )

    def test_weekend_dinner_may_take_its_time(self) -> None:
        recipes = [self._recipe(1, "Долгое блюдо", minutes=120)]
        plan = self._plan(
            recipes, starts_on=date(2026, 8, 29), plan_profile={"weekday_max_minutes": 45}
        )
        self.assertEqual(plan["meals"][0]["recipe_id"], 1)

    def test_relaxed_limit_is_reported_instead_of_an_empty_slot(self) -> None:
        recipes = [self._recipe(1, "Долгое блюдо", minutes=80)]
        plan = self._plan(recipes, plan_profile={"weekday_max_minutes": 45})
        self.assertEqual(len(plan["meals"]), 1)
        self.assertTrue(
            any("time_limit_relaxed" in warning for warning in plan["warnings"]),
            plan["warnings"],
        )

    def test_recipe_without_known_time_is_not_dropped(self) -> None:
        recipes = [self._recipe(1, "Блюдо без времени", minutes=None)]
        plan = self._plan(recipes, plan_profile={"weekday_max_minutes": 45})
        self.assertEqual(len(plan["meals"]), 1)


class MultiCuisineTests(unittest.TestCase):
    """У рецепта несколько кухонь, «universal» проходит любой фильтр (TZ-M8).

    Владелец оставил кухню жёстким фильтром, поэтому кухня должна быть у
    каждого рецепта: борщ честно и русский, и восточноевропейский, а
    универсальная выпечка не притворяется ничьей.
    """

    @staticmethod
    def _recipe(recipe_id: int, meal: str, codes: list[str] | None, title: str) -> dict:
        item = recipe(recipe_id, title, meal)
        item["cuisine_code"] = codes[0] if codes else None
        item["cuisine_codes"] = codes or []
        return item

    def _plan(self, recipes: list[dict], cuisines: list[str], mode: str = "only") -> dict:
        return build_plan(
            household_id="household",
            starts_on=date(2026, 8, 28),
            days=1,
            cuisines=cuisines,
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=recipes,
            products=[],
            cuisine_mode=mode,
            meals=["dinner"],
        )

    def test_second_cuisine_of_a_recipe_counts(self) -> None:
        """Борщ с двумя кухнями попадает в выбор «восточноевропейская»."""
        recipes = [
            self._recipe(1, "dinner", ["russian", "east_european"], "Борщ"),
            self._recipe(2, "dinner", ["italian"], "Паста карбонара"),
        ]
        plan = self._plan(recipes, ["east_european"])
        self.assertEqual(plan["meals"][0]["title"], "Борщ")
        self.assertNotIn("cuisine_fallback", plan["meals"][0]["warnings"])

    def test_universal_recipe_passes_any_hard_filter(self) -> None:
        recipes = [self._recipe(1, "dinner", ["universal"], "Овощное рагу")]
        plan = self._plan(recipes, ["japanese"])
        self.assertEqual(plan["meals"][0]["title"], "Овощное рагу")
        self.assertNotIn("cuisine_fallback", plan["meals"][0]["warnings"])

    def test_recipe_without_codes_is_treated_as_universal(self) -> None:
        """Разметка ещё не дошла до рецепта — он не выпадает из пула."""
        recipes = [self._recipe(1, "dinner", None, "Овсяная каша")]
        plan = self._plan(recipes, ["georgian"])
        self.assertEqual(len(plan["meals"]), 1)

    def test_foreign_cuisine_is_marked_when_it_is_the_only_option(self) -> None:
        recipes = [self._recipe(1, "dinner", ["italian"], "Паста карбонара")]
        plan = self._plan(recipes, ["japanese"])
        self.assertIn("cuisine_fallback", plan["meals"][0]["warnings"])

    def _slot_candidates(self, recipes: list[dict], mode: str) -> list[int]:
        captured: dict = {}

        def spy_optimize(**kwargs):
            captured["slots"] = kwargs["candidates_by_slot"]
            return {}, "greedy"

        with patch("app.web.planning.optimizer.optimize", side_effect=spy_optimize):
            self._plan(recipes, ["japanese"], mode=mode)
        return captured["slots"][0, "dinner"]

    def test_only_mode_hides_other_cuisines_from_the_solver(self) -> None:
        """Жёсткий режим: пока японских блюд хватает, других солвер не видит."""
        recipes = [
            self._recipe(1, "dinner", ["italian"], "Паста карбонара"),
            self._recipe(2, "dinner", ["japanese"], "Мисо суп"),
        ]
        self.assertEqual(self._slot_candidates(recipes, "only"), [2])

    def test_prefer_mode_keeps_other_cuisines_as_candidates(self) -> None:
        """Мягкий режим: чужая кухня остаётся в пуле, просто без бонуса."""
        recipes = [
            self._recipe(1, "dinner", ["italian"], "Паста карбонара"),
            self._recipe(2, "dinner", ["japanese"], "Мисо суп"),
        ]
        self.assertEqual(sorted(self._slot_candidates(recipes, "prefer")), [1, 2])


class FamilyCostTests(unittest.TestCase):
    """TZ-M8 T1 (дефект P6): цена кандидата — на семью и за вычетом запасов.

    Раньше солвер сравнивал «книжную» стоимость рецепта, а найденный дома
    продукт обнулял ингредиент целиком — двести миллилитров молока в
    холодильнике делали бесплатным литр, который нужен на четверых.
    """

    PRODUCTS = [
        {
            "id": 1, "name": "Молоко питьевое 3,2%", "pack_quantity": Decimal("930"),
            "pack_unit": "ml", "effective_price_kop": 9300,
            "category_slugs": ["moloko-syr-yaytsa"],
        },
    ]

    @staticmethod
    def _recipe() -> dict:
        item = recipe(1, "Молочный суп", "dinner", "Молоко")
        item["source_servings_min"] = Decimal("2")
        return item

    def _cost(self, people: int, inventory: list[dict] | None = None) -> int:
        captured: dict = {}

        def spy_optimize(**kwargs):
            captured["scores"] = kwargs["scores"]
            return {}, "greedy"

        with patch("app.web.planning.optimizer.optimize", side_effect=spy_optimize):
            build_plan(
                household_id="household",
                starts_on=date(2026, 8, 28),
                days=1,
                cuisines=[],
                people=[
                    {"name": f"Едок {index}", "person_type": "adult",
                     "portion_factor": Decimal("1")}
                    for index in range(people)
                ],
                appliances=[],
                rules=[],
                inventory=inventory or [],
                recipes=[self._recipe()],
                products=self.PRODUCTS,
            )
        return captured["scores"][1].cost_kop

    def test_cost_scales_with_family_size(self) -> None:
        """Рецепт на две порции для четверых стоит вдвое дороже."""
        self.assertEqual(self._cost(4), 2 * self._cost(2))

    def test_stock_covers_only_what_it_holds(self) -> None:
        """Дома 200 мл из нужных 400 — покупается остаток, а не ничего."""
        stock = [{"name": "Молоко", "quantity": Decimal("200"), "unit_code": "ml",
                  "expires_on": None}]
        full = self._cost(4)
        partial = self._cost(4, stock)
        self.assertGreater(partial, 0)
        self.assertLess(partial, full)
        self.assertAlmostEqual(partial, full // 2, delta=2)

    def test_full_stock_makes_ingredient_free(self) -> None:
        """Литр дома закрывает потребность целиком — ингредиент не покупается."""
        stock = [{"name": "Молоко", "quantity": Decimal("1"), "unit_code": "l",
                  "expires_on": None}]
        self.assertEqual(self._cost(4, stock), 0)


class ApplianceFilterTests(unittest.TestCase):
    """Техника — фильтр всегда (TZ-M8 T1, дефект P8).

    Прежнее поведение (A2: пустой список техники отключал фильтр целиком)
    пропускало в меню рецепты для мультиварки и гриля тем, у кого их нет.
    Новый пользователь не остаётся заблокированным: при регистрации семья
    получает DEFAULT_APPLIANCES.
    """

    @staticmethod
    def _recipes(appliances: list[str]) -> list[dict]:
        recipes = []
        for index in range(1, 4):
            for offset, meal in ((0, "breakfast"), (10, "lunch"), (20, "dinner")):
                item = recipe(index + offset, f"Блюдо {index + offset}", meal)
                item["appliances"] = appliances
                recipes.append(item)
        return recipes

    @staticmethod
    def _plan(recipes: list[dict], appliances: list[str]) -> dict:
        return build_plan(
            household_id="household",
            starts_on=date(2026, 8, 17),
            days=3,
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=appliances,
            rules=[],
            inventory=[],
            recipes=recipes,
            products=[],
        )

    def test_empty_appliances_still_filter(self) -> None:
        """Пустая техника — не «разрешено всё»: рецепт с требованиями отсеян."""
        with self.assertRaises(ValueError):
            self._plan(self._recipes(["oven", "blender"]), [])

    def test_recipes_without_requirements_pass_with_empty_appliances(self) -> None:
        """Рецепт, которому ничего не нужно, проходит при любой технике."""
        plan = self._plan(self._recipes([]), [])
        self.assertEqual(len(plan["meals"]), 9)

    def test_default_appliances_cover_usual_recipes(self) -> None:
        """DEFAULT_APPLIANCES из регистрации закрывают плиту и духовку."""
        plan = self._plan(self._recipes(["oven"]), sorted(DEFAULT_APPLIANCES))
        self.assertEqual(len(plan["meals"]), 9)

    def test_declared_appliances_still_filter(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(self._recipes(["oven"]), ["stove"])

    def test_declared_appliances_accept_matching_recipes(self) -> None:
        plan = self._plan(self._recipes(["stove"]), ["stove", "oven"])
        self.assertEqual(len(plan["meals"]), 9)


class HotPathMemoTests(unittest.TestCase):
    """N1: «Собрать меню» подвисало на десятки секунд.

    Профиль холодной генерации: 21 из 22 секунд — внутри ``ProductMatcher.match``,
    и 14 из них уходили на ``_food_token_key`` (2 млн вызовов, 28 млн
    ``startswith``). Разбор слова зависит только от самого слова, поэтому
    считается один раз на слово, а не на каждую пару «ингредиент × товар».
    """

    PRODUCTS = [
        {
            "id": index,
            "name": name,
            "pack_quantity": 500,
            "pack_unit": "g",
            "effective_price_kop": 5000 + index,
            "category_slugs": ["ovoshchi-frukty-1"],
        }
        for index, name in enumerate(
            [
                "Картофель мытый",
                "Морковь мытая",
                "Лук репчатый",
                "Капуста белокочанная",
                "Свёкла столовая",
                "Перец сладкий красный",
                "Томаты сливовидные",
                "Огурцы гладкие",
                "Кабачки молодые",
                "Баклажаны свежие",
            ],
            start=1,
        )
    ]
    INGREDIENTS = [
        "картофель", "морковь", "лук репчатый", "капуста", "свёкла",
        "перец сладкий", "томаты", "огурцы", "кабачки", "баклажаны",
    ]

    def test_word_key_is_computed_once_per_word(self) -> None:
        _food_token_key.cache_clear()
        first = _food_token_key("картофель")
        second = _food_token_key("картофель")
        self.assertEqual(first, second)
        info = _food_token_key.cache_info()
        self.assertEqual(info.misses, 1, "слово разбирается один раз")
        self.assertGreaterEqual(info.hits, 1)

    def test_product_names_are_parsed_once_per_name(self) -> None:
        """Сопоставление 10 ингредиентов по каталогу из 10 товаров не должно
        разбирать имена по 100 раз — иначе на 500 рецептах это десятки секунд."""
        matcher = ProductMatcher(self.PRODUCTS)
        _tokens.cache_clear()
        _normal.cache_clear()
        for name in self.INGREDIENTS:
            matcher.match(name, "g", "balanced", Decimal("200"))
        misses = _tokens.cache_info().misses
        self.assertLessEqual(
            misses,
            len(self.PRODUCTS) + len(self.INGREDIENTS),
            f"имена разбирались повторно: {misses} разборов на "
            f"{len(self.PRODUCTS)} товаров и {len(self.INGREDIENTS)} ингредиентов",
        )

    def test_warm_up_fills_matcher_cache_for_planner_recipes(self) -> None:
        """Прогрев повторяет ключи, по которым потом спрашивает планировщик:
        после него генерация не платит за сопоставления (N1, холодный кэш)."""
        recipes = [
            {
                "id": index,
                "ingredients": [
                    {
                        "normalized_name": name,
                        "quantity_min": Decimal("200"),
                        "quantity_max": Decimal("200"),
                        "unit_code": "g",
                    }
                ],
            }
            for index, name in enumerate(self.INGREDIENTS, start=1)
        ]
        matcher = ProductMatcher(self.PRODUCTS)
        warmed = warm_product_matcher(matcher, recipes, "balanced")
        self.assertEqual(warmed, len(self.INGREDIENTS))
        filled = len(matcher.cache)
        self.assertGreater(filled, 0)
        for recipe_row in recipes:
            _ingredient_cost_hint(recipe_row["ingredients"][0], matcher, "balanced")
        self.assertEqual(
            len(matcher.cache), filled, "после прогрева новых сопоставлений быть не должно"
        )


MEAL_WORDS = {"breakfast": "утреннее", "lunch": "дневное", "dinner": "вечернее"}


class CuisineFilterTests(unittest.TestCase):
    """Выбранная кухня — фильтр, а не пожелание.

    До правки «Азиатская» проигрывала цене: русские блины из дешёвой муки
    обходили креветки на 3500 единиц целевой функции против бонуса кухни в 200.
    """

    PRODUCTS = [
        {
            "id": 1, "name": "Мука пшеничная высший сорт", "pack_quantity": 1000,
            "pack_unit": "g", "effective_price_kop": 5000,
            "category_slugs": ["makarony-krupy-muka-25"],
        },
        {
            "id": 2, "name": "Креветки королевские", "pack_quantity": 500,
            "pack_unit": "g", "effective_price_kop": 60000,
            "category_slugs": ["ryba-moreprodukty-30"],
        },
    ]

    @staticmethod
    def _recipe(recipe_id: int, title: str, meal_type: str, cuisine: str, ingredient: str) -> dict:
        return {
            "id": recipe_id,
            "title": title,
            "source_page_start": recipe_id,
            "source_servings_min": Decimal("2"),
            "cuisine_code": cuisine,
            "meal_types": [meal_type],
            "appliances": [],
            "review_status": "ready",
            "extraction_confidence": Decimal("0.9"),
            "source_title": "Тестовая книга",
            "ingredients": [
                {
                    "ingredient_text": ingredient,
                    "normalized_name": ingredient,
                    "quantity_min": Decimal("300"),
                    "quantity_max": Decimal("300"),
                    "unit_code": "g",
                }
            ],
        }

    def _recipes(self, asian_meals: tuple[str, ...]) -> list[dict]:
        recipes = []
        recipe_id = 1
        for meal_type in ("breakfast", "lunch", "dinner"):
            for index in range(10):
                recipes.append(self._recipe(
                    recipe_id, f"Русское блюдо {MEAL_WORDS[meal_type]} {index}", meal_type,
                    "russian", "мука пшеничная",
                ))
                recipe_id += 1
            if meal_type in asian_meals:
                for index in range(10):
                    recipes.append(self._recipe(
                        recipe_id, f"Азиатское блюдо {MEAL_WORDS[meal_type]} {index}", meal_type,
                        "asian", "креветки",
                    ))
                    recipe_id += 1
        return recipes

    def _plan(self, asian_meals: tuple[str, ...]) -> dict:
        return build_plan(
            household_id="household",
            starts_on=date(2026, 8, 20),
            days=3,
            cuisines=["asian"],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=self._recipes(asian_meals),
            products=self.PRODUCTS,
        )

    def test_selected_cuisine_wins_over_cheaper_dish(self) -> None:
        plan = self._plan(("breakfast", "lunch", "dinner"))
        self.assertEqual(len(plan["meals"]), 9)
        titles = [meal["title"] for meal in plan["meals"]]
        self.assertTrue(
            all(title.startswith("Азиатское") for title in titles),
            f"в план попало блюдо другой кухни: {titles}",
        )

    def test_slot_without_selected_cuisine_is_filled_and_marked(self) -> None:
        """Азиатских завтраков нет — слот заполняется, но честно помечается."""
        plan = self._plan(("lunch", "dinner"))
        breakfasts = [meal for meal in plan["meals"] if meal["meal_type"] == "breakfast"]
        self.assertEqual(len(breakfasts), 3, "слот не должен оставаться пустым")
        for meal in breakfasts:
            self.assertIn("cuisine_fallback", meal["warnings"])
        for meal in plan["meals"]:
            if meal["meal_type"] != "breakfast":
                self.assertNotIn("cuisine_fallback", meal["warnings"])

    def test_alternatives_keep_selected_cuisine(self) -> None:
        alternatives = slot_alternatives(
            meal_date=date(2026, 8, 20),
            meal_type="lunch",
            current_recipe_id=None,
            other_meals=[],
            cuisines=["asian"],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=self._recipes(("breakfast", "lunch", "dinner")),
            products=self.PRODUCTS,
            limit=10,
        )
        self.assertEqual(len(alternatives), 10)
        self.assertTrue(
            all(recipe["cuisine_code"] == "asian" for recipe in alternatives),
            "замена предлагает блюда другой кухни",
        )


class DishVarietyTests(unittest.TestCase):
    """«На завтрак почему-то только блины».

    Блины — самая дешёвая группа завтраков (медиана 156 ₽ против 458 ₽ у
    основных блюд), а цена весит в целевой функции на порядок больше
    разнообразия. Один тип блюда не должен занимать все слоты приёма пищи.
    """

    PRODUCTS = [
        {
            "id": 1, "name": "Мука пшеничная высший сорт", "pack_quantity": 1000,
            "pack_unit": "g", "effective_price_kop": 5000,
            "category_slugs": ["makarony-krupy-muka-25"],
        },
        {
            "id": 2, "name": "Крупа овсяная", "pack_quantity": 1000,
            "pack_unit": "g", "effective_price_kop": 40000,
            "category_slugs": ["makarony-krupy-muka-25"],
        },
        {
            "id": 3, "name": "Филе куриное охлаждённое", "pack_quantity": 1000,
            "pack_unit": "g", "effective_price_kop": 90000,
            "category_slugs": ["myaso-ptitsa-27"],
        },
    ]
    KINDS = {
        "pancakes": ("Блинчики", "мука пшеничная"),
        "porridge": ("Каша", "крупа овсяная"),
        "main_course": ("Куриное блюдо", "филе куриное"),
    }

    def _recipes(self) -> list[dict]:
        recipes = []
        recipe_id = 1
        for meal_type, word in (
            ("breakfast", "утреннее"), ("lunch", "дневное"), ("dinner", "вечернее"),
        ):
            for dish_type, (title, ingredient) in self.KINDS.items():
                for index in range(6):
                    recipes.append({
                        "id": recipe_id,
                        "title": f"{title} {word} {index}",
                        "source_page_start": recipe_id,
                        "source_servings_min": Decimal("2"),
                        "cuisine_code": "russian",
                        "meal_types": [meal_type],
                        "dish_type": dish_type,
                        "appliances": [],
                        "review_status": "ready",
                        "extraction_confidence": Decimal("0.9"),
                        "source_title": "Тестовая книга",
                        "ingredients": [{
                            "ingredient_text": ingredient,
                            "normalized_name": ingredient,
                            "quantity_min": Decimal("300"),
                            "quantity_max": Decimal("300"),
                            "unit_code": "g",
                        }],
                    })
                    recipe_id += 1
        return recipes

    def _plan(self, days: int = 3) -> dict:
        return build_plan(
            household_id="household",
            starts_on=date(2026, 8, 20),
            days=days,
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=self._recipes(),
            products=self.PRODUCTS,
        )

    def test_cheapest_dish_type_does_not_take_every_breakfast(self) -> None:
        plan = self._plan()
        titles = [meal["title"] for meal in plan["meals"] if meal["meal_type"] == "breakfast"]
        self.assertEqual(len(titles), 3)
        pancakes = [title for title in titles if title.startswith("Блинчики")]
        self.assertLessEqual(
            len(pancakes), 1, f"один тип блюда занял почти весь приём пищи: {titles}"
        )

    def test_alternatives_offer_different_dish_types(self) -> None:
        alternatives = slot_alternatives(
            meal_date=date(2026, 8, 20),
            meal_type="breakfast",
            current_recipe_id=None,
            other_meals=[],
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=self._recipes(),
            products=self.PRODUCTS,
            limit=6,
        )
        kinds = {recipe.get("dish_type") for recipe in alternatives}
        self.assertEqual(
            len(kinds), 3, f"в замене только {kinds}: список не показывает разные блюда"
        )


if __name__ == "__main__":
    unittest.main()
