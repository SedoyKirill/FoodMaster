"""Telegram-бот (app.telegram): привязка чата, ответы и форматирование."""

import asyncio
import json
import os
import sys
import unittest
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool

from app.telegram.dispatch import handle_message
from app.telegram.render import HELP_TEXT, format_day, format_week
from app.telegram.repository import BotRepository

try:  # httpx нужен только транспорту; без него пропускаем его тесты
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

TODAY = date(2026, 8, 18)
PLAN_ID = str(uuid.uuid4())


class LinkAccountTests(unittest.TestCase):
    def test_valid_token_links_account(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "UPDATE app_core.one_time_tokens", {"user_id": "u-1"})
        pool.on("fetchval", "SELECT login FROM app_core.users", "vanya")
        repository = BotRepository(pool)
        login = asyncio.run(repository.link_user(7, "raw-token"))
        self.assertEqual(login, "vanya")
        # Старые связи личности и пользователя удаляются перед вставкой.
        delete_sql, delete_args = pool.first_matching("DELETE FROM app_core.auth_identities")
        self.assertIn("7", delete_args)
        insert_sql, insert_args = pool.first_matching("INSERT INTO app_core.auth_identities")
        # TZ-M7 §3.1: пишем Telegram from.id, а не chat_id
        self.assertEqual(insert_args, ("7", "u-1"))

    def test_expired_token_returns_none(self) -> None:
        pool = FakePool()  # fetchrow по умолчанию отдаёт None
        repository = BotRepository(pool)
        self.assertIsNone(asyncio.run(repository.link_user(7, "bad")))
        self.assertEqual(pool.count_matching("INSERT INTO app_core.auth_identities"), 0)


class _StubRepository:
    """Репозиторий с заранее заданными ответами для handle_message."""

    def __init__(self, context=None, meals=None, shopping=None, link_result=None):
        self.context = context
        self.meals = meals or []
        self.shopping = shopping or []
        self.link_result = link_result
        self.link_calls: list[tuple[int, str]] = []

    async def link_user(self, user_id, raw_token):
        self.link_calls.append((user_id, raw_token))
        return self.link_result

    async def context_for_user(self, user_id):
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
        reply = asyncio.run(handle_message(repository, 42, "/start link_abc123", TODAY, app_repository=None, dialogs=None))
        self.assertEqual(repository.link_calls, [(42, "abc123")])
        self.assertIn("vanya", reply.text)

    def test_start_with_bad_token_explains_how_to_relink(self) -> None:
        repository = _StubRepository(link_result=None)
        reply = asyncio.run(handle_message(repository, 42, "/start link_expired", TODAY, app_repository=None, dialogs=None))
        self.assertIn("просрочен", reply.text)

    def test_unlinked_user_is_offered_an_account(self) -> None:
        """T4: непривязанному предлагаем завести аккаунт прямо здесь."""
        repository = _StubRepository(context=None)
        reply = asyncio.run(handle_message(
            repository, 42, "Сегодня", TODAY, app_repository=None, dialogs=None
        ))
        self.assertIn("Супостат", reply.text)
        self.assertIsNotNone(reply.keyboard)

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
        reply = asyncio.run(handle_message(repository, 42, "🍽 Сегодня", TODAY, app_repository=None, dialogs=None))
        self.assertIn("Завтрак: Каша", reply.text)
        self.assertIn("Ужин: Суп", reply.text)
        self.assertIn("по 1 из 2 блюд", reply.text)  # честная неполнота ккал
        rows = reply.keyboard["inline_keyboard"]
        self.assertEqual(len(rows), 2)  # по ряду [📖][🔁] на блюдо
        self.assertEqual(len(rows[0]), 2)

    def test_unknown_command_shows_help(self) -> None:
        repository = _StubRepository(context={
            "household_id": "h-1", "user_id": "u-1", "role": "owner",
            "login": "vanya", "household_name": "Моя семья",
        })
        reply = asyncio.run(handle_message(
            repository, 42, "борщ??", TODAY, app_repository=None, dialogs=None
        ))
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



# --- транспорт (TZ-M7 T1) ----------------------------------------------------

class _Recorder:
    """Обработчик httpx.MockTransport: журналирует вызовы Bot API."""

    def __init__(self, responses: dict | None = None) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.requests.append((method, payload))
        queue = self.responses.get(method)
        if queue:
            status, body = queue.pop(0)
            return httpx.Response(status, json=body)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 100 + len(self.requests)}}
        )

    def calls(self, method: str) -> list[dict]:
        return [payload for name, payload in self.requests if name == method]


def _with_client(recorder, scenario):
    """Запустить сценарий с TelegramClient поверх MockTransport (без сети)."""
    async def run():
        transport = httpx.MockTransport(recorder)
        async with httpx.AsyncClient(transport=transport) as http:
            from app.telegram.bot import TelegramClient

            return await scenario(TelegramClient("test-token", http))

    return asyncio.run(run())


@unittest.skipIf(httpx is None, "httpx не установлен")
class TelegramClientTests(unittest.TestCase):
    def test_send_message_attaches_menu_by_default(self) -> None:
        from app.telegram.bot import KEYBOARD

        recorder = _Recorder()
        _with_client(recorder, lambda client: client.send_message(42, "привет"))
        payload = recorder.calls("sendMessage")[0]
        self.assertEqual(payload["reply_markup"], KEYBOARD)

    def test_send_message_without_markup_when_none(self) -> None:
        recorder = _Recorder()
        _with_client(recorder, lambda client: client.send_message(42, "⏳ Ищу…", None))
        payload = recorder.calls("sendMessage")[0]
        # T1 §4.5: плейсхолдер уходит вообще без клавиатуры
        self.assertNotIn("reply_markup", payload)

    def test_send_message_marks_only_last_chunk(self) -> None:
        recorder = _Recorder()
        keyboard = {"inline_keyboard": [[{"text": "тык", "callback_data": "c|1"}]]}
        _with_client(
            recorder, lambda client: client.send_message(42, "а\n\n" * 3000, keyboard)
        )
        payloads = recorder.calls("sendMessage")
        self.assertGreater(len(payloads), 1)
        self.assertNotIn("reply_markup", payloads[0])
        self.assertEqual(payloads[-1]["reply_markup"], keyboard)

    def test_edit_ignores_not_modified(self) -> None:
        recorder = _Recorder({"editMessageText": [
            (400, {"ok": False, "description": "Bad Request: message is not modified"}),
        ]})
        result = _with_client(
            recorder, lambda client: client.edit_message_text(42, 55, "тот же текст")
        )
        self.assertEqual(result, 55)
        self.assertEqual(recorder.calls("sendMessage"), [])

    def test_edit_falls_back_to_new_message_on_400(self) -> None:
        recorder = _Recorder({"editMessageText": [
            (400, {"ok": False, "description": "Bad Request: message can't be edited"}),
        ]})
        result = _with_client(
            recorder, lambda client: client.edit_message_text(42, 55, "результат замены")
        )
        sends = recorder.calls("sendMessage")
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["text"], "результат замены")
        self.assertNotEqual(result, 55)  # вернулся id нового сообщения

    def test_edit_splits_long_text(self) -> None:
        recorder = _Recorder()
        _with_client(
            recorder, lambda client: client.edit_message_text(42, 55, "б\n\n" * 3000)
        )
        edits = recorder.calls("editMessageText")
        sends = recorder.calls("sendMessage")
        self.assertEqual(len(edits), 1)
        self.assertGreaterEqual(len(sends), 1)
        for payload in edits + sends:
            self.assertLessEqual(len(payload["text"]), 4096)

    def test_api_error_carries_and_logs_description(self) -> None:
        from app.telegram.bot import TelegramApiError

        recorder = _Recorder({"sendMessage": [
            (403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}),
        ]})
        with self.assertLogs("ration.telegram", level="WARNING") as logs:
            with self.assertRaises(TelegramApiError) as caught:
                _with_client(recorder, lambda client: client.send_message(42, "привет"))
        self.assertIn("blocked by the user", caught.exception.description)
        self.assertTrue(any("blocked by the user" in line for line in logs.output))

    def test_api_error_is_httpx_error(self) -> None:
        from app.telegram.bot import TelegramApiError

        # цикл опроса ловит httpx.HTTPError — новая ошибка не должна его миновать
        self.assertTrue(issubclass(TelegramApiError, httpx.HTTPError))

    def test_set_my_commands_uses_private_scope(self) -> None:
        from app.telegram.bot import BOT_COMMANDS

        recorder = _Recorder()
        _with_client(recorder, lambda client: client.set_my_commands(BOT_COMMANDS))
        payload = recorder.calls("setMyCommands")[0]
        self.assertEqual(payload["scope"], {"type": "all_private_chats"})
        self.assertEqual(len(payload["commands"]), len(BOT_COMMANDS))
        self.assertEqual(payload["commands"][0]["command"], "start")

    def test_set_my_commands_failure_does_not_raise(self) -> None:
        recorder = _Recorder({"setMyCommands": [
            (400, {"ok": False, "description": "Bad Request: BOT_COMMAND_INVALID"}),
        ]})
        _with_client(recorder, lambda client: client.set_my_commands([("ой", "плохо")]))
        self.assertEqual(len(recorder.calls("setMyCommands")), 1)


if __name__ == "__main__":
    unittest.main()
