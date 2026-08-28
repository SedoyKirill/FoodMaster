"""Напоминания бота (TZ-M7 T10, §6). Часы инъектируются — тесты не спят."""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool, repository_with_pool

from app.telegram import notifications
from app.telegram.callbacks import encode_callback, parse_callback
from app.telegram.notifications import (
    EXPIRING, MORNING, PLAN_ENDS, SHOPPING, Notifier, expiring_reply, is_due,
    morning_reply, plan_ends_reply, setting_for, shopping_reply,
)
from app.telegram.scenes import settings

TG_ID = 88112250
HOUSEHOLD = uuid.uuid4()
USER = uuid.uuid4()
PLAN_ID = uuid.uuid4()
TODAY = date(2026, 9, 3)


def run_async(coro):
    return asyncio.run(coro)


def at(hour, minute=0, day=TODAY):
    return datetime(day.year, day.month, day.day, hour, minute)


def meal(meal_type="lunch", *, title="Борщ", day=TODAY, kcal=680):
    return {
        "id": str(uuid.uuid4()), "meal_date": day.isoformat(), "meal_type": meal_type,
        "recipe_id": 7, "title": title, "estimated_kcal": kcal, "warnings": [],
    }


def plan(*, starts_on=date(2026, 9, 1), days=3, meals=None, shopping=None):
    return {
        "id": str(PLAN_ID), "starts_on": starts_on.isoformat(), "days": days,
        "meals": meals if meals is not None else [
            meal("breakfast", title="Сырники"), meal("lunch"), meal("dinner", title="Плов"),
        ],
        "shopping": shopping or [],
    }


def item(name="молоко", *, bought=False, cost=8900):
    return {
        "id": str(uuid.uuid4()), "normalized_name": name,
        "buy_quantity": Decimal("1"), "unit_code": "l", "pack_count": 1,
        "estimated_cost_kop": cost,
        "purchased_at": "2026-09-01T10:00:00" if bought else None,
        "to_taste": False, "category_slug": None,
    }


class _BotRepo:
    def __init__(self, *, targets=None, stored=None, lots=None, dishes=None):
        self.targets = targets if targets is not None else [{
            "telegram_id": TG_ID, "user_id": USER, "login": "tg7",
            "household_id": HOUSEHOLD, "household_name": "Моя семья",
            "timezone": "Europe/Moscow", "role": "owner",
        }]
        self.stored = stored or {}
        self.lots = lots or []
        self.dishes = dishes or {}
        self.marked = []
        self.saved = []

    async def notification_targets(self):
        return self.targets

    async def notification_settings(self, telegram_id):
        return dict(self.stored)

    async def set_notification(self, telegram_id, kind, enabled, hour):
        self.saved.append((telegram_id, kind, enabled, hour))
        self.stored[kind] = {"kind": kind, "enabled": enabled, "hour": hour,
                             "last_sent_on": None}

    async def mark_notified(self, telegram_id, kind, day):
        self.marked.append((telegram_id, kind, day))
        row = dict(self.stored.get(kind) or {})
        row["last_sent_on"] = day
        self.stored[kind] = row

    async def expiring_lots(self, household_id, until):
        return [lot for lot in self.lots
                if date.fromisoformat(lot["expires_on"]) <= until]

    async def dishes_using(self, plan_id, names):
        return {name: titles for name, titles in self.dishes.items() if name in names}


class _AppRepo:
    def __init__(self, latest=None):
        self.latest = latest

    async def latest_plan(self, session):
        return self.latest


class _Sender:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, reply):
        self.sent.append((chat_id, reply))


class ScheduleTests(unittest.TestCase):
    def test_defaults_follow_the_spec(self) -> None:
        """А6: по умолчанию включены утреннее меню и сроки."""
        self.assertEqual(setting_for(MORNING, {})[:2], (True, 8))
        self.assertEqual(setting_for(EXPIRING, {})[:2], (True, 9))
        self.assertFalse(setting_for(SHOPPING, {})[0])
        self.assertFalse(setting_for(PLAN_ENDS, {})[0])

    def test_not_due_before_the_hour(self) -> None:
        self.assertFalse(is_due(MORNING, {}, at(7, 59)))
        self.assertTrue(is_due(MORNING, {}, at(8, 0)))

    def test_catches_up_after_downtime(self) -> None:
        """Бот лежал в восемь — меню всё равно придёт, но один раз."""
        self.assertTrue(is_due(MORNING, {}, at(10, 30)))

    def test_not_sent_twice_a_day(self) -> None:
        stored = {MORNING: {"enabled": True, "hour": 8, "last_sent_on": TODAY}}
        self.assertFalse(is_due(MORNING, stored, at(20, 0)))

    def test_yesterday_does_not_block_today(self) -> None:
        stored = {MORNING: {"enabled": True, "hour": 8,
                            "last_sent_on": TODAY - timedelta(days=1)}}
        self.assertTrue(is_due(MORNING, stored, at(8, 0)))

    def test_iso_string_from_database_is_understood(self) -> None:
        stored = {MORNING: {"enabled": True, "hour": 8, "last_sent_on": TODAY.isoformat()}}
        self.assertFalse(is_due(MORNING, stored, at(9, 0)))

    def test_disabled_kind_never_fires(self) -> None:
        stored = {MORNING: {"enabled": False, "hour": 8, "last_sent_on": None}}
        self.assertFalse(is_due(MORNING, stored, at(23, 0)))

    def test_custom_hour(self) -> None:
        stored = {MORNING: {"enabled": True, "hour": 11, "last_sent_on": None}}
        self.assertFalse(is_due(MORNING, stored, at(10, 0)))
        self.assertTrue(is_due(MORNING, stored, at(11, 0)))


class TextTests(unittest.TestCase):
    def test_morning_lists_today(self) -> None:
        reply = morning_reply(plan(), TODAY)
        self.assertIn("Сырники", reply.text)
        self.assertIn("Борщ", reply.text)
        verbs = [parse_callback(b["callback_data"])[0]
                 for row in reply.keyboard["inline_keyboard"] for b in row]
        self.assertEqual(verbs.count("r"), 3)
        self.assertEqual(verbs.count("x"), 3)

    def test_morning_is_silent_without_meals_today(self) -> None:
        self.assertIsNone(morning_reply(plan(meals=[meal(day=date(2026, 9, 9))]), TODAY))

    def test_expiring_shows_when_and_where(self) -> None:
        lots = [
            {"name": "молоко", "quantity": 1, "unit_code": "l",
             "expires_on": TODAY.isoformat(), "storage_area": "fridge"},
            {"name": "творог", "quantity": 200, "unit_code": "g",
             "expires_on": (TODAY + timedelta(days=2)).isoformat(), "storage_area": "fridge"},
        ]
        reply = expiring_reply(lots, TODAY, {"молоко": ["Сырники", "Блины"]})
        self.assertIn("молоко — сегодня", reply.text)
        self.assertIn("творог — через 2 дн.", reply.text)
        self.assertIn("в плане: Сырники, Блины", reply.text)

    def test_expiring_is_silent_without_lots(self) -> None:
        self.assertIsNone(expiring_reply([], TODAY))

    def test_shopping_when_less_than_half_bought(self) -> None:
        items = [item("молоко"), item("яйцо"), item("хлеб", bought=True)]
        reply = shopping_reply(plan(shopping=items))
        self.assertIn("не куплено 2 из 3", reply.text)
        self.assertIn("178 ₽", reply.text)

    def test_shopping_silent_when_mostly_bought(self) -> None:
        items = [item("молоко", bought=True), item("яйцо", bought=True), item("хлеб")]
        self.assertIsNone(shopping_reply(plan(shopping=items)))

    def test_shopping_silent_when_all_bought(self) -> None:
        items = [item("молоко", bought=True)]
        self.assertIsNone(shopping_reply(plan(shopping=items)))

    def test_shopping_silent_without_list(self) -> None:
        self.assertIsNone(shopping_reply(plan(shopping=[])))

    def test_plan_ends_only_on_the_last_day(self) -> None:
        last = date(2026, 9, 3)  # старт 1 сентября, три дня
        self.assertIsNotNone(plan_ends_reply(plan(), last))
        self.assertIsNone(plan_ends_reply(plan(), last - timedelta(days=1)))
        self.assertIsNone(plan_ends_reply(plan(), last + timedelta(days=1)))

    def test_plan_ends_offers_the_wizard(self) -> None:
        reply = plan_ends_reply(plan(), TODAY)
        data = reply.keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(parse_callback(data), ("n", ["pl", "new"]))


class NotifierTests(unittest.TestCase):
    def _notifier(self, bot_repository, app_repository, sender, moment):
        return Notifier(bot_repository, app_repository, sender, clock=lambda: moment)

    def test_morning_is_sent_once(self) -> None:
        bot_repository = _BotRepo()
        sender = _Sender()
        notifier = self._notifier(bot_repository, _AppRepo(plan()), sender, at(8, 5))
        self.assertEqual(run_async(notifier.tick()), 1)
        self.assertIn("Сырники", sender.sent[0][1].text)
        self.assertEqual(bot_repository.marked[0][:2], (TG_ID, MORNING))
        # второй проход в тот же день молчит
        self.assertEqual(run_async(notifier.tick()), 0)

    def test_nothing_before_eight(self) -> None:
        sender = _Sender()
        notifier = self._notifier(_BotRepo(), _AppRepo(plan()), sender, at(7, 0))
        self.assertEqual(run_async(notifier.tick()), 0)
        self.assertEqual(sender.sent, [])

    def test_expiring_uses_horizon_and_dishes(self) -> None:
        bot_repository = _BotRepo(
            lots=[
                {"name": "молоко", "expires_on": (TODAY + timedelta(days=1)).isoformat(),
                 "quantity": 1, "unit_code": "l", "storage_area": "fridge"},
                {"name": "рис", "expires_on": (TODAY + timedelta(days=30)).isoformat(),
                 "quantity": 1, "unit_code": "kg", "storage_area": "pantry"},
            ],
            dishes={"молоко": ["Сырники"]},
            stored={MORNING: {"enabled": False, "hour": 8, "last_sent_on": None}},
        )
        sender = _Sender()
        notifier = self._notifier(bot_repository, _AppRepo(plan()), sender, at(9, 30))
        run_async(notifier.tick())
        text = sender.sent[0][1].text
        self.assertIn("молоко", text)
        self.assertNotIn("рис", text)  # тридцать дней — не повод писать
        self.assertIn("Сырники", text)

    def test_shopping_only_on_the_eve(self) -> None:
        items = [item("молоко"), item("яйцо")]
        stored = {
            MORNING: {"enabled": False, "hour": 8, "last_sent_on": None},
            EXPIRING: {"enabled": False, "hour": 9, "last_sent_on": None},
            SHOPPING: {"enabled": True, "hour": 18, "last_sent_on": None},
        }
        # план стартует завтра — пишем
        eve = _BotRepo(stored=dict(stored))
        sender = _Sender()
        run_async(self._notifier(
            eve, _AppRepo(plan(starts_on=TODAY + timedelta(days=1), shopping=items)),
            sender, at(18, 10),
        ).tick())
        self.assertIn("Завтра начинается меню", sender.sent[0][1].text)

        # план стартует через неделю — молчим
        later = _BotRepo(stored=dict(stored))
        quiet = _Sender()
        run_async(self._notifier(
            later, _AppRepo(plan(starts_on=TODAY + timedelta(days=7), shopping=items)),
            quiet, at(18, 10),
        ).tick())
        self.assertEqual(quiet.sent, [])

    def test_nothing_to_say_is_not_marked(self) -> None:
        """Плана нет — не помечаем день отправленным, вдруг появится к обеду."""
        bot_repository = _BotRepo()
        notifier = self._notifier(bot_repository, _AppRepo(None), _Sender(), at(8, 5))
        self.assertEqual(run_async(notifier.tick()), 0)
        self.assertEqual(bot_repository.marked, [])

    def test_unlinked_users_are_not_notified(self) -> None:
        """После отвязки строки в auth_identities нет — адресат исчезает сам."""
        sender = _Sender()
        notifier = self._notifier(_BotRepo(targets=[]), _AppRepo(plan()), sender, at(8, 5))
        self.assertEqual(run_async(notifier.tick()), 0)

    def test_one_broken_target_does_not_stop_the_rest(self) -> None:
        class Broken(_BotRepo):
            async def notification_settings(self, telegram_id):
                if telegram_id == 1:
                    raise RuntimeError("боль")
                return {}

        bot_repository = Broken(targets=[
            {"telegram_id": 1, "user_id": USER, "household_id": HOUSEHOLD,
             "timezone": "Europe/Moscow", "role": "owner", "login": "a",
             "household_name": "A"},
            {"telegram_id": 2, "user_id": USER, "household_id": HOUSEHOLD,
             "timezone": "Europe/Moscow", "role": "owner", "login": "b",
             "household_name": "B"},
        ])
        sender = _Sender()
        notifier = self._notifier(bot_repository, _AppRepo(plan()), sender, at(8, 5))
        with self.assertLogs("ration.telegram", level="ERROR"):
            self.assertEqual(run_async(notifier.tick()), 1)
        self.assertEqual(sender.sent[0][0], 2)

    def test_household_timezone_is_used(self) -> None:
        """08:05 по Владивостоку — это ещё вчерашний вечер в Москве."""
        target = {
            "telegram_id": TG_ID, "user_id": USER, "login": "tg7",
            "household_id": HOUSEHOLD, "household_name": "Моя семья",
            "timezone": "Asia/Vladivostok", "role": "owner",
        }
        moment = datetime(2026, 9, 3, 22, 5, tzinfo=ZoneInfo("Europe/Moscow"))
        sender = _Sender()
        notifier = Notifier(
            _BotRepo(targets=[target]), _AppRepo(plan(meals=[meal(day=date(2026, 9, 4))])),
            sender, clock=lambda: moment,
        )
        run_async(notifier.tick())
        # во Владивостоке уже 4 сентября, 5 утра — до восьми, значит молчим
        self.assertEqual(sender.sent, [])

    def test_broken_timezone_falls_back(self) -> None:
        target = {
            "telegram_id": TG_ID, "user_id": USER, "login": "tg7",
            "household_id": HOUSEHOLD, "household_name": "Моя семья",
            "timezone": "Марс/Олимп", "role": "owner",
        }
        moment = datetime(2026, 9, 3, 8, 5, tzinfo=ZoneInfo("Europe/Moscow"))
        sender = _Sender()
        notifier = Notifier(
            _BotRepo(targets=[target]), _AppRepo(plan()), sender, clock=lambda: moment
        )
        self.assertEqual(run_async(notifier.tick()), 1)


class SettingsScreenTests(unittest.TestCase):
    def test_screen_lists_every_kind(self) -> None:
        reply = run_async(settings.notifications_reply(_BotRepo(), TG_ID))
        labels = [b["text"] for row in reply.keyboard["inline_keyboard"] for b in row]
        for kind in notifications.KINDS.values():
            self.assertTrue(any(kind.title in label for label in labels), kind.title)
        self.assertIn("✅ 🍽 Утреннее меню", labels)
        self.assertIn("☐ 🛒 Напоминание о закупке", labels)

    def test_toggle_writes_the_setting(self) -> None:
        bot_repository = _BotRepo()
        run_async(settings.toggle_notification(bot_repository, TG_ID, SHOPPING))
        self.assertEqual(bot_repository.saved[0], (TG_ID, SHOPPING, True, 18))
        run_async(settings.toggle_notification(bot_repository, TG_ID, SHOPPING))
        self.assertEqual(bot_repository.saved[1], (TG_ID, SHOPPING, False, 18))

    def test_unknown_kind_is_rejected(self) -> None:
        result = run_async(settings.toggle_notification(_BotRepo(), TG_ID, "звонок"))
        self.assertEqual(result.toast, "Не понял кнопку.")

    def test_buttons_fit_in_64_bytes(self) -> None:
        reply = run_async(settings.notifications_reply(_BotRepo(), TG_ID))
        for row in reply.keyboard["inline_keyboard"]:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)


class RepositorySqlTests(unittest.TestCase):
    def test_targets_skip_blocked_accounts(self) -> None:
        from app.telegram.repository import BotRepository

        pool = FakePool()
        run_async(BotRepository(pool).notification_targets())
        sql, _ = pool.first_matching("FROM app_core.auth_identities")
        self.assertIn("u.status='active'", sql)
        self.assertIn("provider='telegram'", sql)

    def test_mark_notified_upserts(self) -> None:
        from app.telegram.repository import BotRepository

        pool = FakePool()
        run_async(BotRepository(pool).mark_notified(TG_ID, MORNING, TODAY))
        sql, args = pool.first_matching("INSERT INTO app_core.telegram_notifications")
        self.assertIn("ON CONFLICT (user_id, kind) DO UPDATE", sql)
        self.assertEqual(args, (TG_ID, MORNING, TODAY))

    def test_expiring_lots_are_bounded_by_date(self) -> None:
        from app.telegram.repository import BotRepository

        pool = FakePool()
        run_async(BotRepository(pool).expiring_lots(HOUSEHOLD, TODAY))
        sql, args = pool.first_matching("FROM app_core.inventory_lots")
        self.assertIn("expires_on <= $2", sql)
        self.assertEqual(args, (HOUSEHOLD, TODAY))

    def test_dishes_lookup_goes_through_recipes(self) -> None:
        from app.telegram.repository import BotRepository

        pool = FakePool()
        pool.on("fetch", "FROM app_core.plan_meals", [
            {"name": "молоко", "title": "Сырники"},
            {"name": "молоко", "title": "Блины"},
        ])
        found = run_async(BotRepository(pool).dishes_using(PLAN_ID, ["молоко"]))
        self.assertEqual(found, {"молоко": ["Сырники", "Блины"]})

    def test_dishes_lookup_skips_empty_request(self) -> None:
        from app.telegram.repository import BotRepository

        pool = FakePool()
        self.assertEqual(run_async(BotRepository(pool).dishes_using(PLAN_ID, [])), {})
        self.assertEqual(pool.calls, [])

    def test_unlink_clears_notification_settings(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "DELETE FROM app_core.auth_identities",
                {"provider_user_id": str(TG_ID)})
        session = {"user_id": USER, "household_id": HOUSEHOLD, "channel": "telegram"}
        self.assertTrue(run_async(repository_with_pool(pool).unlink_telegram(session)))
        _, args = pool.first_matching("DELETE FROM app_core.telegram_notifications")
        self.assertEqual(args, (TG_ID,))


if __name__ == "__main__":
    unittest.main()
