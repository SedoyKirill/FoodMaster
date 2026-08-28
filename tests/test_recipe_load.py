import unittest
from decimal import Decimal

from app.recipes.pipeline import _candidate_from_json, _dedupe, _window_body


def make_recipe_json(**overrides):
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
            {
                "raw_text": "200 г муки",
                "name": "мука",
                "quantity_min": 200,
                "quantity_max": 200,
                "unit": "g",
                "is_to_taste": False,
                "section": None,
                "note": None,
            },
            {
                "raw_text": "соль по вкусу",
                "name": "соль",
                "quantity_min": None,
                "quantity_max": None,
                "unit": None,
                "is_to_taste": True,
                "section": None,
                "note": None,
            },
        ],
        "steps": [
            "Смешать муку с солью и молоком до однородного теста.",
            "Выпекать блины на разогретой сковороде с двух сторон.",
        ],
    }
    base.update(overrides)
    return base


class CandidateTests(unittest.TestCase):
    def test_ready_candidate(self) -> None:
        candidate = _candidate_from_json(make_recipe_json(), "текст окна", "Книга")
        self.assertEqual(candidate.review_status, "ready")
        self.assertEqual(candidate.extraction_method, "llm")
        self.assertEqual(candidate.source_servings_min, Decimal("4"))
        self.assertTrue(candidate.ingredients[1].is_to_taste)
        self.assertIsNone(candidate.cuisine_code)

    def test_needs_review_candidate(self) -> None:
        recipe = make_recipe_json(servings_min=None, servings_max=None, yield_text=None)
        candidate = _candidate_from_json(recipe, "текст окна", None)
        self.assertEqual(candidate.review_status, "needs_review")
        self.assertIn("no_yield", candidate.review_reasons)

    def test_quantities_become_decimal(self) -> None:
        recipe = make_recipe_json()
        recipe["ingredients"][0]["quantity_min"] = 1.5
        candidate = _candidate_from_json(recipe, "", None)
        self.assertEqual(candidate.ingredients[0].quantity_min, Decimal("1.5"))


class DedupeTests(unittest.TestCase):
    def test_same_title_adjacent_pages_deduped(self) -> None:
        complete = _candidate_from_json(make_recipe_json(page_start=5), "", None)
        truncated = _candidate_from_json(
            make_recipe_json(page_start=6, is_complete=False, steps=[
                "Смешать муку с солью и молоком до однородного теста.",
            ]),
            "",
            None,
        )
        kept = _dedupe([truncated, complete])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].page_start, 5)

    def test_different_titles_kept(self) -> None:
        first = _candidate_from_json(make_recipe_json(), "", None)
        second = _candidate_from_json(make_recipe_json(title="Оладьи на кефире"), "", None)
        self.assertEqual(len(_dedupe([first, second])), 2)

    def test_same_title_far_pages_kept(self) -> None:
        first = _candidate_from_json(make_recipe_json(page_start=5), "", None)
        second = _candidate_from_json(make_recipe_json(page_start=40), "", None)
        self.assertEqual(len(_dedupe([first, second])), 2)


class WindowBodyTests(unittest.TestCase):
    def test_yaml_header_stripped(self) -> None:
        import tempfile
        from pathlib import Path

        content = "---\nwindow_id: 001-0001-0004\nsha256: abc\n---\n=== СТРАНИЦА 1 ===\nтекст\n"
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "001-0001-0004.md"
            path.write_text(content, encoding="utf-8")
            self.assertEqual(_window_body(path), "=== СТРАНИЦА 1 ===\nтекст")


if __name__ == "__main__":
    unittest.main()
