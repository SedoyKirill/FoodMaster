"""Telegram-бот (app.telegram): привязка чата, ответы и форматирование."""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool

from app.telegram.service import (
    BotRepository, HELP_TEXT, NOT_LINKED_TEXT,
    format_day, format_shopping, format_week, handle_message,
)

TODAY = date(2026, 8, 18)
PLAN_ID = str(uuid.uuid4())


class LinkChatTests(unittest.TestCase):
    def test_valid_token_links_chat(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "UPDATE app_core.one_time_tokens", {"user_id": "u-1"})
        pool.on("fetchval", "SELECT login FROM app_core.users", "vanya")
        repository = BotRepository(pool)
        login = asyncio.run(repository.link_chat(42, "raw-token"))
        self.assertEqual(login, "vanya")
        # Старые связи чата и пользователя удаляются перед вставкой.
        delete_sql, delete_args = pool.first_matching("DELETE FROM app_core.auth_identities")
        self.assertIn("42", delete_args)
        insert_sql, insert_args = pool.first_matching("INSERT INTO app_core.auth_identities")
        self.assertEqual(insert_args, ("42", "u-1"))

    def test_expired_token_returns_none(self) -> None:
        pool = FakePool()  # fetchrow по умолчанию отдаёт None
        repository = BotRepository(pool)
        self.assertIsNone(asyncio.run(repository.link_chat(42, "bad")))
        self.assertEqual(pool.count_matching("INSERT INTO app_core.auth_identities"), 0)


class _StubRepository:
    """Репозиторий с заранее заданными ответами для handle_message."""

    def __init__(self, context=None, meals=None, shopping=None, link_result=None):
        self.context = context
        self.meals = meals or []
        self.shopping = shopping or []
        self.link_result = link_result
        self.link_calls: list[tuple[int, str]] = []

    async def link_chat(self, chat_id, raw_token):
        self.link_calls.append((chat_id, raw_token))
        return self.link_result

    async def context_for_chat(self, chat_id):
        return self.context

    async def latest_plan_meals(self, household_id):
        return self.meals

    async def shopping_items(self, household_id):
        return self.shopping


def _meal(**overrides):
    base = {
        "meal_date": TODAY, "meal_type": "breakfast",
        "estimated_kcal": 500, "position": 1, "title": "Каша",
    }
    base.update(overrides)
    return base


class HandleMessageTests(unittest.TestCase):
    def test_start_with_link_payload_calls_link(self) -> None:
        repository = _StubRepository(link_result="vanya")
        reply = asyncio.run(handle_message(repository, 42, "/start link_abc123", TODAY))
        self.assertEqual(repository.link_calls, [(42, "abc123")])
        self.assertIn("vanya", reply.text)

    def test_start_with_bad_token_explains_how_to_relink(self) -> None:
        repository = _StubRepository(link_result=None)
        reply = asyncio.run(handle_message(repository, 42, "/start link_expired", TODAY))
        self.assertIn("просрочен", reply.text)

    def test_unlinked_chat_gets_instructions(self) -> None:
        repository = _StubRepository(context=None)
        reply = asyncio.run(handle_message(repository, 42, "Сегодня", TODAY))
        self.assertEqual(reply.text, NOT_LINKED_TEXT)

    def test_today_returns_menu_with_buttons(self) -> None:
        repository = _StubRepository(
            context={"household_id": "h-1", "login": "vanya"},
            meals=[
                _meal(id=str(uuid.uuid4()), plan_id=PLAN_ID),
                _meal(
                    id=str(uuid.uuid4()), plan_id=PLAN_ID,
                    meal_type="dinner", title="Суп", estimated_kcal=None,
                ),
            ],
        )
        reply = asyncio.run(handle_message(repository, 42, "🍽 Сегодня", TODAY))
        self.assertIn("Завтрак: Каша", reply.text)
        self.assertIn("Ужин: Суп", reply.text)
        self.assertIn("по 1 из 2 блюд", reply.text)  # честная неполнота ккал
        rows = reply.keyboard["inline_keyboard"]
        self.assertEqual(len(rows), 2)  # по ряду [📖][🔁] на блюдо
        self.assertEqual(len(rows[0]), 2)

    def test_unknown_command_shows_help(self) -> None:
        repository = _StubRepository(context={"household_id": "h-1"})
        reply = asyncio.run(handle_message(repository, 42, "борщ??", TODAY))
        self.assertIn(HELP_TEXT, reply.text)


class FormattingTests(unittest.TestCase):
    def test_format_day_empty(self) -> None:
        self.assertIn("блюд в плане нет", format_day([], TODAY))

    def test_format_week_groups_by_date(self) -> None:
        meals = [
            _meal(),
            _meal(meal_date=date(2026, 8, 19), meal_type="lunch", title="Плов"),
        ]
        text = format_week(meals)
        self.assertIn("вторник, 18 августа", text)
        self.assertIn("среда, 19 августа", text)
        self.assertIn("Обед: Плов", text)

    def test_format_shopping_totals_and_skips_purchased(self) -> None:
        items = [
            {
                "normalized_name": "молоко", "buy_quantity": Decimal("930"),
                "unit_code": "ml", "pack_count": 1,
                "estimated_cost_kop": 9900, "purchased_at": None, "to_taste": False,
            },
            {
                "normalized_name": "мука", "buy_quantity": Decimal("1000"),
                "unit_code": "g", "pack_count": 1,
                "estimated_cost_kop": 5000, "purchased_at": "2026-08-18", "to_taste": False,
            },
        ]
        text = format_shopping(items)
        self.assertIn("молоко", text)
        self.assertNotIn("мука", text)  # куплено — не показываем
        self.assertIn("Итого ≈99 ₽", text)

    def test_format_shopping_all_done(self) -> None:
        items = [{
            "normalized_name": "молоко", "buy_quantity": Decimal("930"),
            "unit_code": "ml", "pack_count": 1,
            "estimated_cost_kop": 9900, "purchased_at": "2026-08-18", "to_taste": False,
        }]
        self.assertIn("Всё куплено", format_shopping(items))


if __name__ == "__main__":
    unittest.main()
