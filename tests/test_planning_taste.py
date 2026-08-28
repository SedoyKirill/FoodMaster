"""Модель вкуса семьи: события → аффинити (TZ-M8 §4)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planning.taste import (  # noqa: E402
    EVENT_VALUES, HALF_LIFE_DAYS, RecipeMeta, TasteModel, event_value,
)

TODAY = date(2026, 8, 28)

BORSCH = RecipeMeta(1, dish_type="soup", cuisines=("russian",),
                    ingredients=(("свёкла", 1.0), ("капуста", 0.5)))
SOLYANKA = RecipeMeta(2, dish_type="soup", cuisines=("russian",),
                      ingredients=(("колбаса", 1.0), ("огурец", 0.5)))
PASTA = RecipeMeta(3, dish_type="pasta", cuisines=("italian",),
                   ingredients=(("паста", 1.0), ("сыр", 0.5)))
RECIPES = {meta.recipe_id: meta for meta in (BORSCH, SOLYANKA, PASTA)}


def rated(recipe_id: int, rating: int, day: date = TODAY, person_id=None) -> dict:
    return {
        "recipe_id": recipe_id, "kind": "rated", "value": (rating - 3) / 2,
        "created_at": day, "person_id": person_id,
    }


def event(recipe_id: int, kind: str, day: date = TODAY, person_id=None) -> dict:
    return {
        "recipe_id": recipe_id, "kind": kind, "value": EVENT_VALUES[kind],
        "created_at": day, "person_id": person_id,
    }


class EventValueTests(unittest.TestCase):
    """Каждое действие семьи имеет цену в шкале [-1, 1]."""

    def test_five_stars_is_the_strongest_like(self) -> None:
        self.assertEqual(event_value("rated", 5), 1.0)
        self.assertEqual(event_value("rated", 1), -1.0)
        self.assertEqual(event_value("rated", 3), 0.0)

    def test_replacement_speaks_louder_than_planning(self) -> None:
        self.assertLess(event_value("replaced_out"), 0)
        self.assertGreater(event_value("replaced_in"), 0)
        self.assertEqual(event_value("planned"), 0.0)

    def test_cooked_counts_as_approval_and_skipped_as_refusal(self) -> None:
        self.assertGreater(event_value("cooked"), 0)
        self.assertLess(event_value("skipped"), 0)


class RecipeAffinityTests(unittest.TestCase):
    def test_rated_recipe_gets_positive_affinity(self) -> None:
        model = TasteModel.fit([rated(1, 5)], RECIPES, TODAY)
        self.assertGreater(model.affinity(BORSCH), 0.2)

    def test_unknown_recipe_without_any_signal_is_neutral(self) -> None:
        model = TasteModel.fit([], RECIPES, TODAY)
        self.assertEqual(model.affinity(PASTA), 0.0)

    def test_affinity_stays_inside_the_scale(self) -> None:
        model = TasteModel.fit([rated(1, 5) for _ in range(20)], RECIPES, TODAY)
        self.assertLessEqual(model.affinity(BORSCH), 1.0)

    def test_old_events_fade(self) -> None:
        fresh = TasteModel.fit([rated(1, 5)], RECIPES, TODAY)
        stale = TasteModel.fit(
            [rated(1, 5, date(2026, 8, 28) - __import__("datetime").timedelta(days=HALF_LIFE_DAYS * 3))],
            RECIPES, TODAY,
        )
        self.assertGreater(fresh.affinity(BORSCH), stale.affinity(BORSCH) * 2)

    def test_prior_keeps_a_single_event_modest(self) -> None:
        """Одна пятёрка — ещё не «любимое блюдо семьи»."""
        model = TasteModel.fit([rated(1, 5)], RECIPES, TODAY)
        many = TasteModel.fit([rated(1, 5) for _ in range(5)], RECIPES, TODAY)
        self.assertLess(model.affinity(BORSCH), many.affinity(BORSCH))


class GeneralisationTests(unittest.TestCase):
    """Вкус переносится на тип блюда, кухню и ингредиенты (§4.2)."""

    def test_liking_borsch_lifts_another_soup(self) -> None:
        model = TasteModel.fit([rated(1, 5) for _ in range(3)], RECIPES, TODAY)
        self.assertGreater(model.affinity(SOLYANKA), 0.0)

    def test_generalisation_is_weaker_than_the_dish_itself(self) -> None:
        model = TasteModel.fit([rated(1, 5) for _ in range(3)], RECIPES, TODAY)
        self.assertGreater(model.affinity(BORSCH), model.affinity(SOLYANKA))

    def test_unrelated_dish_is_untouched(self) -> None:
        model = TasteModel.fit([rated(1, 5) for _ in range(3)], RECIPES, TODAY)
        self.assertEqual(model.affinity(PASTA), 0.0)

    def test_replacing_a_soup_twice_teaches_the_type(self) -> None:
        events = [event(1, "replaced_out"), event(1, "replaced_out"), event(2, "replaced_in")]
        model = TasteModel.fit(events, RECIPES, TODAY)
        self.assertLess(model.affinity(BORSCH), 0)
        self.assertGreater(model.affinity(SOLYANKA), model.affinity(BORSCH))


class FamilyCompromiseTests(unittest.TestCase):
    """Блюдо, которое кто-то откровенно не любит, проигрывает (§4.3)."""

    PEOPLE = [
        {"id": "p1", "name": "Мама", "portion_factor": 1},
        {"id": "p2", "name": "Сын", "portion_factor": 1},
    ]

    def test_one_persons_dislike_outweighs_anothers_delight(self) -> None:
        events = [rated(1, 5, person_id="p1") for _ in range(4)]
        events += [rated(1, 1, person_id="p2") for _ in range(4)]
        model = TasteModel.fit(events, RECIPES, TODAY)
        self.assertLess(model.family_affinity(BORSCH, self.PEOPLE), 0)

    def test_without_personal_events_family_score_is_used(self) -> None:
        model = TasteModel.fit([rated(1, 5) for _ in range(3)], RECIPES, TODAY)
        self.assertAlmostEqual(
            model.family_affinity(BORSCH, self.PEOPLE), model.affinity(BORSCH), places=6
        )

    def test_everyone_likes_it(self) -> None:
        events = [rated(1, 5, person_id="p1") for _ in range(3)]
        events += [rated(1, 5, person_id="p2") for _ in range(3)]
        model = TasteModel.fit(events, RECIPES, TODAY)
        self.assertGreater(model.family_affinity(BORSCH, self.PEOPLE), 0.2)


class StorageTests(unittest.TestCase):
    """Аффинити сохраняются построчно — ночной джоб пишет их в таблицу."""

    def test_rows_carry_level_key_score_and_count(self) -> None:
        model = TasteModel.fit([rated(1, 5), rated(1, 4)], RECIPES, TODAY)
        rows = {(row["level"], row["key"]): row for row in model.rows()}
        self.assertIn(("recipe", "1"), rows)
        self.assertIn(("dish_type", "soup"), rows)
        self.assertEqual(rows[("recipe", "1")]["events_count"], 2)
        self.assertLessEqual(abs(rows[("recipe", "1")]["score"]), 1.0)

    def test_summary_lists_favourites_and_dislikes(self) -> None:
        events = [rated(1, 5) for _ in range(3)] + [rated(3, 1) for _ in range(3)]
        summary = TasteModel.fit(events, RECIPES, TODAY).summary(RECIPES)
        self.assertEqual(summary["favourite_recipes"][0]["recipe_id"], 1)
        self.assertEqual(summary["disliked_recipes"][0]["recipe_id"], 3)
        self.assertEqual(summary["favourite_dish_types"][0]["key"], "soup")

    def test_summary_of_empty_history_is_empty(self) -> None:
        summary = TasteModel.fit([], RECIPES, TODAY).summary(RECIPES)
        self.assertEqual(summary["favourite_recipes"], [])
        self.assertEqual(summary["events_count"], 0)


if __name__ == "__main__":
    unittest.main()
