"""Пагинация и лимиты Telegram (TZ-M7 T3, §4.4, приёмка §9.8).

Главное обязательство: лимиты соблюдаются конструктивно — страницами, а не
обрезанием хвоста. Экранные проверки живут в тестах своих сцен, здесь —
сами примитивы: страницы, ряд навигации, сборка клавиатуры, резка текста.
"""

import os
import sys
import unittest
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.telegram.callbacks import parse_callback
from app.telegram.render import (
    MAX_BUTTONS, TELEGRAM_LIMIT, alternatives_keyboard, build_keyboard,
    button_text, chunk_buttons, format_week, pager_row, paginate,
    split_for_telegram,
)

PLAN = uuid.uuid4()
MEAL = uuid.uuid4()



class PaginateTests(unittest.TestCase):
    def test_clamps_page_number(self) -> None:
        # устаревшая кнопка не должна ронять ответ
        self.assertEqual(paginate(range(20), 0, 8).page, 1)
        self.assertEqual(paginate(range(20), 99, 8).page, 3)

    def test_empty_list_is_one_empty_page(self) -> None:
        page = paginate([], 1, 8)
        self.assertEqual((page.pages, page.items, page.total), (1, [], 0))
        self.assertFalse(page.has_prev)
        self.assertFalse(page.has_next)

    def test_last_page_is_partial(self) -> None:
        page = paginate(range(20), 3, 8)
        self.assertEqual(len(page.items), 4)
        self.assertTrue(page.has_prev)
        self.assertFalse(page.has_next)

    def test_chunk_buttons_makes_rows(self) -> None:
        rows = chunk_buttons([{"text": str(i)} for i in range(5)], 2)
        self.assertEqual([len(row) for row in rows], [2, 2, 1])

    def test_button_text_trimmed(self) -> None:
        self.assertEqual(len(button_text("я" * 100)), 60)
        self.assertTrue(button_text("я" * 100).endswith("…"))


class PagerRowTests(unittest.TestCase):
    def test_single_page_has_no_pager(self) -> None:
        self.assertEqual(pager_row("sh", paginate(range(3), 1, 8)), [])

    def test_pager_shows_position_and_navigates(self) -> None:
        row = pager_row("sh", paginate(range(50), 2, 8), "плана")
        labels = [button["text"] for button in row]
        self.assertEqual(labels, ["◀", "2/7", "▶"])
        verb, parts = parse_callback(row[0]["callback_data"])
        self.assertEqual(verb, "p")
        self.assertEqual(parts, ["sh", "плана", "1"])
        self.assertEqual(parse_callback(row[2]["callback_data"])[1][-1], "3")

    def test_counter_button_is_inert(self) -> None:
        row = pager_row("sh", paginate(range(50), 2, 8))
        self.assertEqual(parse_callback(row[1]["callback_data"]), ("n", ["noop"]))


class KeyboardLimitTests(unittest.TestCase):
    def test_build_keyboard_drops_empty_rows(self) -> None:
        self.assertIsNone(build_keyboard([[], []]))

    def test_build_keyboard_rejects_over_hundred(self) -> None:
        rows = [[{"text": str(i), "callback_data": "n:noop"}] for i in range(MAX_BUTTONS + 1)]
        with self.assertRaises(ValueError):
            build_keyboard(rows)

    def test_all_ten_alternatives_are_shown(self) -> None:
        """§5.5: репозиторий отдаёт десять вариантов, а бот показывал три."""
        alternatives = [
            {"recipe_id": index, "title": f"Блюдо {index}"} for index in range(1, 11)
        ]
        rows = alternatives_keyboard(PLAN, MEAL, alternatives)["inline_keyboard"]
        self.assertEqual(len(rows), 11)  # десять вариантов + «оставить как есть»
        self.assertEqual(parse_callback(rows[-1][0]["callback_data"])[0], "c")
        self.assertIn("10. Блюдо 10", rows[9][0]["text"])


class TextLimitTests(unittest.TestCase):
    def test_plan_of_fourteen_days_splits_under_limit(self) -> None:
        """§9.8: план на 14 дней не должен упираться в 4096 символов."""
        start = date(2026, 9, 1)
        meals = []
        for day in range(14):
            for position, meal_type in enumerate(("breakfast", "lunch", "dinner")):
                meals.append({
                    "id": uuid.uuid4(), "plan_id": PLAN,
                    "meal_date": start + timedelta(days=day),
                    "meal_type": meal_type, "position": position,
                    "title": "Плов с бараниной, зирой и жёлтой морковью по-фергански",
                    "estimated_kcal": 720, "estimated_protein": 30,
                    "estimated_fat": 25, "estimated_carb": 80,
                })
        text = format_week(meals)
        self.assertGreater(len(text), TELEGRAM_LIMIT)  # иначе тест ничего не проверяет
        chunks = split_for_telegram(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), TELEGRAM_LIMIT)
        # ни один день не потерялся при разбиении
        self.assertEqual(sum(chunk.count("Плов") for chunk in chunks), 42)


if __name__ == "__main__":
    unittest.main()
