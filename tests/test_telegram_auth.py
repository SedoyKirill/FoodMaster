"""Аккаунт из бота: регистрация, вход в веб по коду, отвязка (TZ-M7 T4, §3.2–3.4)."""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool

from app.telegram.callbacks import encode_callback, parse_callback
from app.telegram.scenes import auth
from app.telegram.dispatch import handle_callback, handle_message
from app.web.database import ConflictError

USER_ID = 7
TODAY = date(2026, 8, 28)
CONTEXT = {
    "user_id": str(uuid.uuid4()), "login": "tg7", "role": "owner",
    "household_id": str(uuid.uuid4()), "household_name": "Моя семья",
}


def run_async(coro):
    return asyncio.run(coro)


class _Dialogs:
    def __init__(self):
        self.state = None
        self.cleared = 0

    async def load(self, user_id):
        return self.state

    async def save(self, user_id, state):
        self.state = state

    async def clear(self, user_id):
        self.cleared += 1
        self.state = None


class _BotRepo:
    """Выборки бота: контекст, привязка, возврат прежнего аккаунта."""

    def __init__(self, context=None, relink=False):
        self.context = context
        self.relink_result = relink
        self.relink_calls = []

    async def context_for_user(self, user_id):
        return self.context

    async def link_user(self, user_id, token):
        return None

    async def relink_account(self, login, user_id):
        self.relink_calls.append((login, user_id))
        return self.relink_result

    async def latest_plan_meals(self, household_id):
        return []

    async def shopping_items(self, household_id):
        return []


class _AppRepo:
    """AppRepository в объёме, который нужен сценам аккаунта."""

    def __init__(self, *, conflict=False, has_password=True, unlinked=True):
        self.conflict = conflict
        self._has_password = has_password
        self.unlinked = unlinked
        self.registered = []
        self.unlink_calls = []

    async def register_account(self, login, password, household_name, **kwargs):
        if self.conflict:
            raise ConflictError("Такой логин уже зарегистрирован")
        self.registered.append((login, password, household_name, kwargs))
        return uuid.uuid4()

    async def web_login_code(self, user_id):
        return "123456"

    async def has_password(self, user_id):
        return self._has_password

    async def unlink_telegram(self, session):
        self.unlink_calls.append(session)
        return self.unlinked


class WelcomeTests(unittest.TestCase):
    def test_start_without_account_offers_registration(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(None), USER_ID, "/start", TODAY,
            app_repository=_AppRepo(), dialogs=_Dialogs(),
        ))
        verbs = [
            parse_callback(button["callback_data"])[1][0]
            for row in reply.keyboard["inline_keyboard"] for button in row
        ]
        self.assertEqual(verbs, ["reg", "link"])

    def test_start_with_account_shows_help(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(CONTEXT), USER_ID, "/start", TODAY,
            app_repository=_AppRepo(), dialogs=_Dialogs(),
        ))
        self.assertIsNone(reply.keyboard)
        self.assertIn("Супостат", reply.text)

    def test_have_account_button_explains_linking(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(), _BotRepo(None), USER_ID, encode_callback("n", "link"), TODAY,
            dialogs=_Dialogs(),
        ))
        self.assertIn("Получить", result.edit.text)

    def test_have_account_button_adds_link_when_username_known(self) -> None:
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_USERNAME": "SoupoStatBot"}):
            reply = auth.have_account_reply()
        self.assertIn("t.me/SoupoStatBot", reply.text)


class RegistrationTests(unittest.TestCase):
    def test_register_button_starts_scene(self) -> None:
        dialogs = _Dialogs()
        result = run_async(handle_callback(
            _AppRepo(), _BotRepo(None), USER_ID, encode_callback("n", "reg"), TODAY,
            dialogs=dialogs,
        ))
        self.assertEqual(dialogs.state.scene, auth.SCENE)
        self.assertEqual(dialogs.state.step, "household")
        self.assertIn("семью", result.edit.text)

    def test_default_name_skips_the_question(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs()
        result = run_async(handle_callback(
            app_repository, _BotRepo(None), USER_ID, encode_callback("n", "regdef"),
            TODAY, dialogs=dialogs,
        ))
        login, password, household, kwargs = app_repository.registered[0]
        self.assertEqual(login, "tg7")
        self.assertIsNone(password)  # §3.2 / А1: аккаунт из бота без пароля
        self.assertEqual(household, auth.DEFAULT_HOUSEHOLD_NAME)
        self.assertEqual(kwargs["telegram_user_id"], USER_ID)
        self.assertEqual(kwargs["channel"], "telegram")
        self.assertIn("создан", result.edit.text)

    def test_scene_step_creates_account_with_typed_name(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs()
        ctx = auth.SceneContext(
            actor=type("A", (), {"user_id": USER_ID, "chat_id": USER_ID})(),
            text="  Семья Ивановых  ", state=None,
            bot_repository=_BotRepo(None), app_repository=app_repository,
            dialogs=dialogs, today=TODAY,
        )
        reply = run_async(auth.handle_step(ctx))
        self.assertEqual(app_repository.registered[0][2], "Семья Ивановых")
        self.assertEqual(dialogs.cleared, 1)  # форма закрыта
        self.assertIn("Семья Ивановых", reply.text)

    def test_empty_name_repeats_the_question(self) -> None:
        app_repository = _AppRepo()
        ctx = auth.SceneContext(
            actor=type("A", (), {"user_id": USER_ID, "chat_id": USER_ID})(),
            text="   ", state=None, bot_repository=_BotRepo(None),
            app_repository=app_repository, dialogs=_Dialogs(), today=TODAY,
        )
        reply = run_async(auth.handle_step(ctx))
        self.assertEqual(app_repository.registered, [])
        self.assertIsNotNone(reply.keyboard)

    def test_existing_login_returns_previous_account(self) -> None:
        """Логин выведен из from.id, значит это тот же человек после отвязки."""
        bot_repository = _BotRepo(None, relink=True)
        reply = run_async(auth.create_account(
            _AppRepo(conflict=True), bot_repository, _Dialogs(), USER_ID, "Моя семья"
        ))
        self.assertEqual(bot_repository.relink_calls, [("tg7", USER_ID)])
        self.assertIn("прежний аккаунт", reply.text)

    def test_login_taken_by_another_chat_explains_linking(self) -> None:
        reply = run_async(auth.create_account(
            _AppRepo(conflict=True), _BotRepo(None, relink=False), _Dialogs(),
            USER_ID, "Моя семья",
        ))
        self.assertIn("Настройки", reply.text)

    def test_linked_user_cannot_register_twice(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(), _BotRepo(CONTEXT), USER_ID, encode_callback("n", "reg"),
            TODAY, dialogs=_Dialogs(),
        ))
        self.assertTrue(result.show_alert)
        self.assertIn("уже привязан", result.toast)


class WebLoginTests(unittest.TestCase):
    def test_web_command_returns_link_and_code(self) -> None:
        with mock.patch.dict(os.environ, {"WEB_PUBLIC_URL": "http://192.168.1.10:8080"}):
            reply = run_async(handle_message(
                _BotRepo(CONTEXT), USER_ID, "/web", TODAY,
                app_repository=_AppRepo(), dialogs=_Dialogs(),
            ))
        self.assertIn("http://192.168.1.10:8080/#/login/tg/123456", reply.text)
        self.assertIn("123456", reply.text)

    def test_web_url_falls_back_to_localhost(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(auth.web_url("1"), "http://localhost:8080/#/login/tg/1")

    def test_web_url_tolerates_trailing_slash(self) -> None:
        with mock.patch.dict(os.environ, {"WEB_PUBLIC_URL": "http://host:8080/"}):
            self.assertEqual(auth.web_url("9"), "http://host:8080/#/login/tg/9")


class UnlinkTests(unittest.TestCase):
    def test_unlink_asks_for_confirmation(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(CONTEXT), USER_ID, "/unlink", TODAY,
            app_repository=_AppRepo(has_password=True), dialogs=_Dialogs(),
        ))
        data = reply.keyboard["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(parse_callback(data), ("y", ["unlink"]))
        self.assertNotIn("нет пароля", reply.text)

    def test_account_without_password_gets_a_warning(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(CONTEXT), USER_ID, "/unlink", TODAY,
            app_repository=_AppRepo(has_password=False), dialogs=_Dialogs(),
        ))
        # §3.4: отвязка разрешена, но человек должен понимать последствия
        self.assertIn("нет пароля", reply.text)
        self.assertIn("/web", reply.text)

    def test_confirmation_unlinks(self) -> None:
        app_repository = _AppRepo(unlinked=True)
        result = run_async(handle_callback(
            app_repository, _BotRepo(CONTEXT), USER_ID, encode_callback("y", "unlink"),
            TODAY, dialogs=_Dialogs(),
        ))
        self.assertEqual(app_repository.unlink_calls[0]["channel"], "telegram")
        self.assertIn("отвязан", result.edit.text)

    def test_unlink_without_link_is_not_an_error(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(unlinked=False), _BotRepo(CONTEXT), USER_ID,
            encode_callback("y", "unlink"), TODAY, dialogs=_Dialogs(),
        ))
        self.assertIn("не был привязан", result.edit.text)


class RelinkSqlTests(unittest.TestCase):
    def test_relink_only_touches_unlinked_account(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "INSERT INTO app_core.auth_identities", {"user_id": "u-1"})
        from app.telegram.repository import BotRepository

        self.assertTrue(run_async(BotRepository(pool).relink_account("tg7", USER_ID)))
        sql, args = pool.first_matching("INSERT INTO app_core.auth_identities")
        self.assertIn("NOT EXISTS", sql)  # занятую привязку не перебиваем
        self.assertEqual(args, ("tg7", str(USER_ID)))


if __name__ == "__main__":
    unittest.main()
