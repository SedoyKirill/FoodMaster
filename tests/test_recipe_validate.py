import unittest

from app.recipes.validate import looks_like_recipe_window, review_recipe, validate_payload


def make_ingredient(**overrides):
    base = {
        "raw_text": "200 г муки",
        "name": "мука",
        "quantity_min": 200,
        "quantity_max": 200,
        "unit": "g",
        "is_to_taste": False,
        "section": None,
        "note": None,
    }
    base.update(overrides)
    return base


def make_recipe(**overrides):
    base = {
        "title": "Блины на молоке",
        "page_start": 12,
        "page_end": 13,
        "is_complete": True,
        "servings_min": 4,
        "servings_max": 4,
        "yield_text": "На 4 порции",
        "time_total_minutes": 30,
        "meal_types": ["breakfast"],
        "diet_tags": [],
        "appliances": ["stove"],
        "ingredients": [
            make_ingredient(),
            make_ingredient(raw_text="2 яйца", name="яйцо", quantity_min=2, quantity_max=2, unit="piece"),
        ],
        "steps": [
            "Смешать муку с яйцами и молоком до однородного теста.",
            "Выпекать блины на разогретой сковороде с двух сторон.",
        ],
    }
    base.update(overrides)
    return base


def make_payload(**overrides):
    base = {
        "window_id": "001-0012-0015",
        "sha256": "a" * 64,
        "model": "haiku-session",
        "schema_version": 1,
        "recipes": [make_recipe()],
    }
    base.update(overrides)
    return base


class ValidatePayloadTests(unittest.TestCase):
    def test_valid_payload_passes(self) -> None:
        self.assertEqual(validate_payload(make_payload()), [])

    def test_empty_recipes_ok(self) -> None:
        self.assertEqual(validate_payload(make_payload(recipes=[])), [])

    def test_bad_unit_reported(self) -> None:
        recipe = make_recipe(ingredients=[make_ingredient(unit="ложка")])
        errors = validate_payload(make_payload(recipes=[recipe]))
        self.assertTrue(any("unit" in error for error in errors))

    def test_missing_field_reported(self) -> None:
        payload = make_payload()
        del payload["sha256"]
        errors = validate_payload(payload)
        self.assertTrue(any(error.startswith("sha256") for error in errors))

    def test_bad_enum_reported(self) -> None:
        recipe = make_recipe(meal_types=["ужин"])
        errors = validate_payload(make_payload(recipes=[recipe]))
        self.assertTrue(any("meal_types" in error for error in errors))

    def test_string_quantity_reported(self) -> None:
        recipe = make_recipe(ingredients=[make_ingredient(quantity_min="200")])
        errors = validate_payload(make_payload(recipes=[recipe]))
        self.assertTrue(any("quantity_min" in error for error in errors))


class ReviewRecipeTests(unittest.TestCase):
    def test_good_recipe_is_ready(self) -> None:
        self.assertEqual(review_recipe(make_recipe(), "Книга о вкусной еде"), [])

    def test_step_as_ingredient(self) -> None:
        recipe = make_recipe(
            ingredients=[
                make_ingredient(),
                make_ingredient(
                    raw_text="Для заправки смешать все ингредиенты",
                    name="для заправки смешать все ингредиенты",
                    quantity_min=None,
                    quantity_max=None,
                    unit=None,
                ),
            ]
        )
        reasons = review_recipe(recipe, None)
        self.assertIn("ingredient_looks_like_step", reasons)

    def test_to_taste_salt_is_fine(self) -> None:
        recipe = make_recipe(
            ingredients=[
                make_ingredient(),
                make_ingredient(
                    raw_text="соль по вкусу", name="соль",
                    quantity_min=None, quantity_max=None, unit=None, is_to_taste=True,
                ),
            ]
        )
        self.assertEqual(review_recipe(recipe, None), [])

    def test_no_yield_flagged(self) -> None:
        recipe = make_recipe(servings_min=None, servings_max=None, yield_text=None)
        self.assertIn("no_yield", review_recipe(recipe, None))

    def test_caps_title_flagged(self) -> None:
        recipe = make_recipe(title="СЛАДОСТИ БЕЗ САХАРА 67ПЕЧЕНЬЕ, ПИРОЖНЫЕ 123456")
        self.assertIn("title_bad", review_recipe(recipe, None))

    def test_book_title_flagged(self) -> None:
        recipe = make_recipe(title="Готовим в аэрогриле")
        self.assertIn("title_is_book_title", review_recipe(recipe, "Готовим в аэрогриле"))

    def test_missing_units_flagged(self) -> None:
        recipe = make_recipe(
            ingredients=[
                make_ingredient(unit=None),
                make_ingredient(name="сахар", unit=None),
                make_ingredient(name="молоко", unit=None),
            ]
        )
        self.assertIn("units_missing", review_recipe(recipe, None))


class RecipeWindowDetectorTests(unittest.TestCase):
    def test_recipe_window_detected(self) -> None:
        body = (
            "=== СТРАНИЦА 38 ===\nКокосовый йогурт\n4 порции\nИнгредиенты:\n"
            "300 г мякоти кокоса\n2 ч. л. сиропа\nСПОСОБ ПРИГОТОВЛЕНИЯ:\nСмешать всё."
        )
        self.assertTrue(looks_like_recipe_window(body))

    def test_intro_text_not_detected(self) -> None:
        body = (
            "=== СТРАНИЦА 3 ===\nПредисловие\nЭта книга родилась из любви к еде "
            "и путешествиям. Автор благодарит семью и друзей за поддержку."
        )
        self.assertFalse(looks_like_recipe_window(body))


if __name__ == "__main__":
    unittest.main()
