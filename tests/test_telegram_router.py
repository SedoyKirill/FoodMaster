"""Роутер бота (TZ-M7 T2): личность по from.id, лимиты частоты, канал аудита."""

import asyncio
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool, StubClock, repository_with_pool

from app.telegram.repository import BotRepository, bot_session
from app.telegram.router import (
    BUSY_TEXT, TOO_FAST_TEXT, Actor, Router, parse_update,
)

USER_ID = 7          # Telegram from.id — личность
CHAT_ID = 42         # id чата: в личке совпал бы, здесь намеренно другой
GROUP_ID = -1001234  # id группы всегда отрицательный


def run_async(coro):
    return asyncio.run(coro)


def _message(text="привет", chat_type="private", user_id=USER_ID, chat_id=CHAT_ID):
    return {
        "update_id": 1,
        "message": {
            "message_id": 5, "text": text,
            "from": {"id": user_id},
            "chat": {"id": chat_id, "type": chat_type},
        },
    }


def _callback(data="c|xx", chat_type="private", with_message=True):
    query = {"id": "cb-1", "data": data, "from": {"id": USER_ID}}
    if with_message:
        query["message"] = {"message_id": 55, "chat": {"id": CHAT_ID, "type": chat_type}}
    return {"update_id": 1, "callback_query": query}


class ParseUpdateTests(unittest.TestCase):
    def test_private_text_gives_actor(self) -> None:
        incoming = parse_update(_message("🍽 Сегодня"))
        self.assertEqual(incoming.kind, "text")
        self.assertEqual(incoming.text, "🍽 Сегодня")
        self.assertEqual(incoming.actor, Actor(USER_ID, CHAT_ID, 5))

    def test_identity_is_from_id_not_chat_id(self) -> None:
        """TZ-M7 §3.1: доступ считается по нажавшему, а не по чату."""
        incoming = parse_update(_message())
        self.assertEqual(incoming.actor.user_id, USER_ID)
        self.assertNotEqual(incoming.actor.user_id, incoming.actor.chat_id)

    def test_group_message_marked_as_group(self) -> None:
        incoming = parse_update(_message(chat_type="supergroup", chat_id=GROUP_ID))
        self.assertEqual(incoming.kind, "group")

    def test_callback_keeps_message_id(self) -> None:
        incoming = parse_update(_callback())
        self.assertEqual(incoming.kind, "callback")
        self.assertEqual(incoming.actor.message_id, 55)
        self.assertEqual(incoming.callback_id, "cb-1")

    def test_callback_without_message_keeps_identity(self) -> None:
        incoming = parse_update(_callback(with_message=False))
        self.assertEqual(incoming.actor.user_id, USER_ID)
        self.assertEqual(incoming.actor.chat_id, 0)  # отвечать некуда — только ack

    def test_group_callback_marked_as_group(self) -> None:
        self.assertEqual(parse_update(_callback(chat_type="group")).kind, "group")

    def test_update_without_sender_is_dropped(self) -> None:
        update = _message()
        del update["message"]["from"]
        self.assertIsNone(parse_update(update))

    def test_update_without_text_is_dropped(self) -> None:
        update = _message()
        del update["message"]["text"]
        self.assertIsNone(parse_update(update))

    def test_service_update_is_dropped(self) -> None:
        self.assertIsNone(parse_update({"update_id": 1, "edited_message": {}}))


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StubClock()
        self.router = Router(clock=self.clock)
        self.actor = Actor(USER_ID, CHAT_ID)

    def test_twenty_messages_per_minute(self) -> None:
        for _ in range(20):
            self.assertTrue(self.router.allow_text(self.actor))
        self.assertFalse(self.router.allow_text(self.actor))
        self.assertEqual(self.router.refusal_text(self.actor), TOO_FAST_TEXT)

    def test_message_budget_refills(self) -> None:
        for _ in range(20):
            self.router.allow_text(self.actor)
        self.assertFalse(self.router.allow_text(self.actor))
        self.clock.advance(60)
        self.assertTrue(self.router.allow_text(self.actor))

    def test_sixty_callbacks_per_minute(self) -> None:
        for _ in range(60):
            self.assertTrue(self.router.allow_callback(self.actor))
        self.assertFalse(self.router.allow_callback(self.actor))

    def test_limits_are_per_user(self) -> None:
        other = Actor(USER_ID + 1, CHAT_ID)
        for _ in range(20):
            self.router.allow_text(self.actor)
        self.assertFalse(self.router.allow_text(self.actor))
        self.assertTrue(self.router.allow_text(other))

    def test_refusal_announced_once_per_window(self) -> None:
        self.assertEqual(self.router.refusal_text(self.actor), TOO_FAST_TEXT)
        # флудеру не отвечаем на каждое сообщение — иначе бот флудит в ответ
        self.assertIsNone(self.router.refusal_text(self.actor))
        self.clock.advance(60)
        self.assertEqual(self.router.refusal_text(self.actor), TOO_FAST_TEXT)

    def test_heavy_once_per_ten_seconds(self) -> None:
        self.assertIsNone(self.router.acquire_heavy(self.actor))
        self.router.release_heavy(self.actor)
        self.assertEqual(self.router.acquire_heavy(self.actor), TOO_FAST_TEXT)
        self.clock.advance(10)
        self.assertIsNone(self.router.acquire_heavy(self.actor))

    def test_single_heavy_operation_per_user(self) -> None:
        """§3.5: одна тяжёлая операция на пользователя, а не на кнопку."""
        self.assertIsNone(self.router.acquire_heavy(self.actor))
        self.assertEqual(self.router.acquire_heavy(self.actor), BUSY_TEXT)
        self.router.release_heavy(self.actor)
        self.clock.advance(10)
        self.assertIsNone(self.router.acquire_heavy(self.actor))

    def test_busy_wins_over_rate_limit(self) -> None:
        # даблклик — не вина пользователя: он не должен съедать окно в 10 с
        self.router.acquire_heavy(self.actor)
        self.assertEqual(self.router.acquire_heavy(self.actor), BUSY_TEXT)

    def test_heavy_limits_are_per_user(self) -> None:
        other = Actor(USER_ID + 1, CHAT_ID)
        self.assertIsNone(self.router.acquire_heavy(self.actor))
        self.assertIsNone(self.router.acquire_heavy(other))


class ContextTests(unittest.TestCase):
    def test_context_query_prefers_active_household(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "FROM app_core.auth_identities", {
            "user_id": uuid.uuid4(), "login": "vanya",
            "household_id": uuid.uuid4(), "household_name": "Моя семья",
            "timezone": "Europe/Moscow", "role": "owner",
        })
        repository = BotRepository(pool)
        context = run_async(repository.context_for_user(USER_ID))
        self.assertEqual(context["role"], "owner")
        sql, args = pool.first_matching("FROM app_core.auth_identities")
        self.assertIn("active_household_id", sql)
        self.assertEqual(args, (str(USER_ID),))  # ищем по from.id, а не по чату

    def test_unknown_identity_gives_none(self) -> None:
        pool = FakePool()  # fetchrow по умолчанию отдаёт None
        self.assertIsNone(run_async(BotRepository(pool).context_for_user(999)))

    def test_link_replaces_previous_identity_of_user(self) -> None:
        """Второй Telegram-аккаунт не может остаться привязанным к тому же логину."""
        pool = FakePool()
        pool.on("fetchrow", "UPDATE app_core.one_time_tokens", {"user_id": "u-1"})
        pool.on("fetchval", "SELECT login FROM app_core.users", "vanya")
        run_async(BotRepository(pool).link_user(USER_ID, "raw"))
        sql, args = pool.first_matching("DELETE FROM app_core.auth_identities")
        self.assertIn("provider_user_id=$1 OR user_id=$2", sql)
        self.assertEqual(args, (str(USER_ID), "u-1"))


class AuditChannelTests(unittest.TestCase):
    """TZ-M7 §3.5: действия бота больше не помечаются каналом 'web'."""

    def _session(self, **overrides):
        return {
            "household_id": uuid.uuid4(), "user_id": uuid.uuid4(),
            "role": "owner", "login": "vanya", **overrides,
        }

    def test_bot_session_carries_channel(self) -> None:
        context = {
            "household_id": "h-1", "user_id": "u-1", "role": "owner",
            "login": "vanya", "household_name": "Моя семья",
        }
        session = bot_session(context)
        self.assertEqual(session["channel"], "telegram")
        # login и household_name нужны get_profile (database.py)
        self.assertEqual(session["login"], "vanya")
        self.assertEqual(session["household_name"], "Моя семья")

    def test_session_channel_reaches_audit_log(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.audit(self._session(channel="telegram"), "recipe.rated"))
        sql, args = pool.first_matching("INSERT INTO app_core.audit_log")
        self.assertIn("telegram", args)
        self.assertNotIn("'web'", sql)  # литерал ушёл из VALUES

    def test_repository_channel_is_default(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool, channel="telegram")
        run_async(repository.audit(self._session(), "inventory.added"))
        _, args = pool.first_matching("INSERT INTO app_core.audit_log")
        self.assertIn("telegram", args)

    def test_web_stays_web(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.audit(self._session(), "inventory.added"))
        _, args = pool.first_matching("INSERT INTO app_core.audit_log")
        self.assertIn("web", args)
        self.assertNotIn("telegram", args)

    def test_unknown_channel_falls_back_to_web(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.audit(self._session(channel="почта"), "inventory.added"))
        _, args = pool.first_matching("INSERT INTO app_core.audit_log")
        self.assertIn("web", args)


class SchemaFilesTests(unittest.TestCase):
    def test_telegram_schema_is_applied_after_web(self) -> None:
        from app.web.database import AppRepository

        files = AppRepository.SCHEMA_FILES
        self.assertIn(("app.web", "schema_telegram.sql"), files)
        # зависит от users/households — значит идёт после основной схемы
        self.assertGreater(
            files.index(("app.web", "schema_telegram.sql")),
            files.index(("app.web", "schema.sql")),
        )


if __name__ == "__main__":
    unittest.main()
