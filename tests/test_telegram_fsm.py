"""Состояние диалога и маршрутизация текста (TZ-M7 T3, §4.2, приёмка §9.7)."""

import asyncio
import json
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool

from app.telegram.fsm import (
    CANCEL_BUTTON, CANCEL_DATA, DialogState, DialogStore, is_cancel, is_expired,
)
from app.telegram.router import Actor, Router

USER_ID = 7
START = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class _Clock:
    """Управляемые часы: тест протухания не должен ждать полчаса."""

    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def run_async(coro):
    return asyncio.run(coro)


class DialogStoreTests(unittest.TestCase):
    def test_save_upserts_state(self) -> None:
        pool = FakePool()
        store = DialogStore(pool, clock=_Clock())
        run_async(store.save(USER_ID, DialogState("plan.new", "days", {"budget": 5000})))
        sql, args = pool.first_matching("INSERT INTO app_core.telegram_dialog_state")
        self.assertIn("ON CONFLICT (user_id) DO UPDATE", sql)
        self.assertEqual(args[0], USER_ID)
        self.assertEqual(args[1], "plan.new")
        self.assertEqual(args[2], "days")
        self.assertEqual(json.loads(args[3]), {"budget": 5000})

    def test_save_keeps_cyrillic_readable(self) -> None:
        pool = FakePool()
        run_async(DialogStore(pool).save(USER_ID, DialogState("s", "step", {"имя": "Маша"})))
        _, args = pool.first_matching("INSERT INTO app_core.telegram_dialog_state")
        self.assertIn("Маша", args[3])

    def test_load_parses_jsonb_string(self) -> None:
        """JSONB без кодека приходит строкой — как в database.py."""
        pool = FakePool()
        pool.on("fetchrow", "FROM app_core.telegram_dialog_state", {
            "scene": "inventory.add", "step": "unit",
            "data": '{"name": "молоко"}', "updated_at": START,
        })
        state = run_async(DialogStore(pool, clock=_Clock()).load(USER_ID))
        self.assertEqual(state.scene, "inventory.add")
        self.assertEqual(state.data, {"name": "молоко"})

    def test_load_within_thirty_minutes(self) -> None:
        clock = _Clock()
        pool = FakePool()
        pool.on("fetchrow", "FROM app_core.telegram_dialog_state", {
            "scene": "plan.new", "step": "days", "data": {}, "updated_at": START,
        })
        clock.advance(minutes=29)
        self.assertIsNotNone(run_async(DialogStore(pool, clock=clock).load(USER_ID)))
        self.assertEqual(pool.count_matching("DELETE FROM app_core.telegram_dialog_state"), 0)

    def test_load_expired_clears_and_returns_none(self) -> None:
        clock = _Clock()
        pool = FakePool()
        pool.on("fetchrow", "FROM app_core.telegram_dialog_state", {
            "scene": "plan.new", "step": "days", "data": {}, "updated_at": START,
        })
        clock.advance(minutes=31)
        self.assertIsNone(run_async(DialogStore(pool, clock=clock).load(USER_ID)))
        self.assertEqual(pool.count_matching("DELETE FROM app_core.telegram_dialog_state"), 1)

    def test_load_without_row_returns_none(self) -> None:
        pool = FakePool()  # fetchrow по умолчанию отдаёт None
        self.assertIsNone(run_async(DialogStore(pool).load(USER_ID)))

    def test_clear_deletes_row(self) -> None:
        pool = FakePool()
        run_async(DialogStore(pool).clear(USER_ID))
        sql, args = pool.first_matching("DELETE FROM app_core.telegram_dialog_state")
        self.assertEqual(args, (USER_ID,))


class CancelTests(unittest.TestCase):
    def test_command_and_button_are_the_same_action(self) -> None:
        self.assertTrue(is_cancel("/cancel"))
        self.assertTrue(is_cancel("✖ Отмена"))
        self.assertTrue(is_cancel("  Отмена  "))
        self.assertFalse(is_cancel("отменить заказ"))

    def test_cancel_button_carries_navigation_verb(self) -> None:
        self.assertEqual(CANCEL_BUTTON["callback_data"], CANCEL_DATA)
        self.assertTrue(CANCEL_DATA.startswith("n:"))

    def test_missing_timestamp_counts_as_expired(self) -> None:
        self.assertTrue(is_expired(None, START))

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        naive = datetime(2026, 8, 28, 11, 59)
        self.assertFalse(is_expired(naive, START))


class _Dialogs:
    """Хранилище форм в памяти — для проверки маршрутизации без БД."""

    def __init__(self, state=None):
        self.state = state
        self.cleared = 0

    async def load(self, user_id):
        return self.state

    async def save(self, user_id, state):
        self.state = state

    async def clear(self, user_id):
        self.cleared += 1
        self.state = None


class RouteTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = Actor(USER_ID, USER_ID)

    def _router(self, state=None, scenes=None):
        self.dialogs = _Dialogs(state)
        return Router(dialogs=self.dialogs, scenes=scenes or {})

    def test_free_text_goes_to_active_scene(self) -> None:
        router = self._router(DialogState("plan.new", "days"), {"plan.new": object()})
        route, state = run_async(router.route_text(self.actor, "5"))
        self.assertEqual(route, "scene")
        self.assertEqual(state.step, "days")

    def test_cancel_clears_form(self) -> None:
        router = self._router(DialogState("plan.new", "days"), {"plan.new": object()})
        route, _ = run_async(router.route_text(self.actor, "/cancel"))
        self.assertEqual(route, "cancel")
        self.assertEqual(self.dialogs.cleared, 1)

    def test_command_interrupts_scene(self) -> None:
        router = self._router(DialogState("plan.new", "days"), {"plan.new": object()})
        route, _ = run_async(router.route_text(self.actor, "/today"))
        self.assertEqual(route, "command")
        self.assertEqual(self.dialogs.cleared, 1)  # из формы можно выйти командой

    def test_menu_button_interrupts_scene(self) -> None:
        router = self._router(DialogState("plan.new", "days"), {"plan.new": object()})
        route, _ = run_async(router.route_text(self.actor, "🛒 Покупки"))
        self.assertEqual(route, "command")
        self.assertEqual(self.dialogs.cleared, 1)

    def test_expired_form_makes_text_a_command(self) -> None:
        """§9.7: через 30 минут ввод не попадает в старую форму."""
        router = self._router(None, {"plan.new": object()})  # load вернул None
        route, _ = run_async(router.route_text(self.actor, "привет"))
        self.assertEqual(route, "command")

    def test_unknown_scene_name_is_ignored(self) -> None:
        router = self._router(DialogState("сцена-которой-нет", "x"), {})
        route, _ = run_async(router.route_text(self.actor, "что-то"))
        self.assertEqual(route, "command")

    def test_without_store_everything_is_a_command(self) -> None:
        route, _ = run_async(Router().route_text(self.actor, "привет"))
        self.assertEqual(route, "command")


if __name__ == "__main__":
    unittest.main()
