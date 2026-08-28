"""Контекст горизонта: календарь, сезон, история, время готовки (TZ-M8 §3.5–3.7)."""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planning.context import (  # noqa: E402
    HISTORY_WINDOW_DAYS, DayContext, PlanHistory, build_history, day_context,
    is_holiday, season_of, season_share, slot_time_limit,
)


class CalendarTests(unittest.TestCase):
    """Будни и выходные: в среду на готовку меньше времени, чем в субботу."""

    def test_weekday_and_weekend_are_distinguished(self) -> None:
        self.assertTrue(day_context(date(2026, 8, 26)).weekday)   # среда
        self.assertFalse(day_context(date(2026, 8, 29)).weekday)  # суббота

    def test_public_holiday_counts_as_weekend(self) -> None:
        new_year = day_context(date(2027, 1, 4))  # понедельник новогодних каникул
        self.assertTrue(new_year.holiday)
        self.assertFalse(new_year.weekday)

    def test_holiday_moved_from_weekend_frees_the_next_workday(self) -> None:
        """23 февраля 2026 — понедельник, а 8 марта 2026 — воскресенье."""
        self.assertTrue(is_holiday(date(2026, 2, 23)))
        self.assertTrue(is_holiday(date(2026, 3, 8)))
        self.assertTrue(is_holiday(date(2026, 3, 9)))  # перенос с воскресенья

    def test_ordinary_day_is_not_a_holiday(self) -> None:
        self.assertFalse(is_holiday(date(2026, 8, 26)))


class SeasonTests(unittest.TestCase):
    """Сезонность нужна вкусу и разнообразию, цену Лента отражает сама."""

    def test_season_of_month(self) -> None:
        self.assertEqual(season_of(date(2026, 7, 15)), "summer")
        self.assertEqual(season_of(date(2026, 1, 15)), "winter")
        self.assertEqual(season_of(date(2026, 10, 15)), "autumn")

    def test_summer_vegetables_score_in_august(self) -> None:
        share = season_share(["кабачок", "укроп", "рис"], date(2026, 8, 20))
        self.assertAlmostEqual(share, 2 / 3, places=3)

    def test_same_dish_is_not_seasonal_in_february(self) -> None:
        self.assertEqual(season_share(["кабачок", "укроп"], date(2026, 2, 20)), 0.0)

    def test_dish_without_ingredients_scores_zero(self) -> None:
        self.assertEqual(season_share([], date(2026, 8, 20)), 0.0)


class HistoryTests(unittest.TestCase):
    """Что ели три недели назад, планировщик обязан помнить (§3.7)."""

    ROWS = [
        {"recipe_id": 1, "meal_date": date(2026, 8, 27), "dish_type": "soup",
         "main_ingredient": "картофель"},
        {"recipe_id": 2, "meal_date": date(2026, 8, 20), "dish_type": "pancakes",
         "main_ingredient": "мука"},
        {"recipe_id": 3, "meal_date": date(2026, 7, 1), "dish_type": "soup",
         "main_ingredient": "свёкла"},
    ]

    def setUp(self) -> None:
        self.history = build_history(self.ROWS, date(2026, 8, 28))

    def test_days_since_counts_from_plan_start(self) -> None:
        self.assertEqual(self.history.days_since(1), 1)
        self.assertEqual(self.history.days_since(2), 8)

    def test_older_than_window_is_forgotten(self) -> None:
        self.assertIsNone(self.history.days_since(3))
        self.assertIsNone(self.history.days_since(999))

    def test_recency_penalty_fades_over_three_weeks(self) -> None:
        self.assertAlmostEqual(self.history.recency_penalty(1), 20 / HISTORY_WINDOW_DAYS, places=3)
        self.assertAlmostEqual(self.history.recency_penalty(2), 13 / HISTORY_WINDOW_DAYS, places=3)
        self.assertEqual(self.history.recency_penalty(999), 0.0)

    def test_favourite_dish_is_penalised_for_a_week_only(self) -> None:
        """Любимое возвращается раз в неделю, остальное — раз в три (§3.7)."""
        week_old = build_history(
            [{"recipe_id": 5, "meal_date": date(2026, 8, 21), "dish_type": None,
              "main_ingredient": None}],
            date(2026, 8, 28),
        )
        self.assertGreater(week_old.recency_penalty(5), 0)
        self.assertEqual(week_old.recency_penalty(5, affinity=0.9), 0.0)

    def test_recent_dish_types_and_ingredients_are_collected(self) -> None:
        self.assertEqual(self.history.recent_dish_types["soup"], 1)
        self.assertEqual(self.history.recent_main_ingredients["картофель"], 1)
        self.assertNotIn("свёкла", self.history.recent_main_ingredients)

    def test_dates_arriving_as_iso_strings_still_count(self) -> None:
        """Репозиторий отдаёт даты строками — история их принимает.

        До этого ротация молча не работала на живой базе: ``row_dict``
        приводит даты к ISO, а история ждала ``date`` и отбрасывала всё.
        """
        history = build_history(
            [{"recipe_id": 7, "meal_date": "2026-08-18", "dish_type": "soup"}],
            date(2026, 8, 20),
        )
        self.assertEqual(history.days_since(7), 2)
        self.assertGreater(history.recency_penalty(7), 0.0)

    def test_broken_date_string_is_ignored_not_fatal(self) -> None:
        history = build_history(
            [{"recipe_id": 7, "meal_date": "позавчера"}], date(2026, 8, 20)
        )
        self.assertIsNone(history.days_since(7))

    def test_empty_history_is_harmless(self) -> None:
        empty = PlanHistory.empty()
        self.assertIsNone(empty.days_since(1))
        self.assertEqual(empty.recency_penalty(1), 0.0)


class CookingTimeTests(unittest.TestCase):
    """Лимит времени зависит от приёма и от того, будни это или выходной."""

    PROFILE = {
        "weekday_max_minutes": 45,
        "weekend_max_minutes": None,
        "breakfast_max_minutes": 25,
    }

    def test_weekday_dinner_limited(self) -> None:
        wednesday = DayContext(date(2026, 8, 26), weekday=True, holiday=False, season="summer")
        self.assertEqual(slot_time_limit("dinner", wednesday, self.PROFILE), 45)

    def test_weekend_dinner_unlimited_by_default(self) -> None:
        saturday = DayContext(date(2026, 8, 29), weekday=False, holiday=False, season="summer")
        self.assertIsNone(slot_time_limit("dinner", saturday, self.PROFILE))

    def test_breakfast_has_its_own_limit_on_weekdays(self) -> None:
        wednesday = DayContext(date(2026, 8, 26), weekday=True, holiday=False, season="summer")
        self.assertEqual(slot_time_limit("breakfast", wednesday, self.PROFILE), 25)

    def test_breakfast_on_weekend_is_not_rushed(self) -> None:
        sunday = DayContext(date(2026, 8, 30), weekday=False, holiday=False, season="summer")
        self.assertIsNone(slot_time_limit("breakfast", sunday, self.PROFILE))


if __name__ == "__main__":
    unittest.main()
