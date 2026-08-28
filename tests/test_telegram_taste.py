"""Вкусы семьи из чата (TZ-M7 T11, §5.10).

Модель вкуса живёт в TZ-M8 и до бота доезжает отдельно, поэтому здесь два
набора проверок: как сцена ведёт себя без неё и как — с ней.
"""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.telegram.bot import BOT_COMMANDS, commands_for
from app.telegram.callbacks import CALLBACK_LIMIT, encode_callback, parse_callback
from app.telegram.dispatch import handle_callback, handle_message
from app.telegram.fsm import DialogState
from app.telegram.render import TELEGRAM_LIMIT
from app.telegram.scenes import SceneContext, settings, taste
from app.telegram.router import Actor

USER_ID = 7
TODAY = date(2026, 8, 28)
OWNER = {
    "user_id": str(uuid.uuid4()), "login": "tg7", "role": "owner",
    "household_id": str(uuid.uuid4()), "household_name": "Моя семья",
    "channel": "telegram",
}
VIEWER = {**OWNER, "role": "viewer"}


def run_async(coro):
    return asyncio.run(coro)


def card(recipe_id, title, cuisine="georgian", dish="soup"):
    return {"recipe_id": recipe_id, "title": title, "cuisine_code": cuisine,
            "dish_type": dish, "source_page_start": 12}


class _Dialogs:
    def __init__(self, state=None):
        self.state = state

    async def load(self, user_id):
        return self.state

    async def save(self, user_id, state):
        self.state = state

    async def clear(self, user_id):
        self.state = None


class _TasteRepo:
    """Репозиторий с моделью вкуса — контракт TZ-M8 §4.4."""

    def __init__(self, *, cards=None, events=0, summary=None):
        self.cards = cards if cards is not None else [
            card(101, "Харчо"), card(202, "Лобио", dish="main_course"),
            card(303, "Оливье", cuisine="russian", dish="salad"),
        ]
        self.events = events
        self._summary = summary
        self.recorded = []

    async def taste_onboarding(self, session):
        answered = {recipe_id for recipe_id, _, _ in self.recorded}
        return {
            "events_count": self.events,
            "needed": self.events < 10,
            "cards": [item for item in self.cards
                      if item["recipe_id"] not in answered],
        }

    async def record_taste_event(self, session, recipe_id, kind, *, rating=None,
                                 person_id=None, channel="web", connection=None):
        self.recorded.append((int(recipe_id), kind, channel))
        self.events += 1

    async def taste_summary(self, session):
        if self._summary is not None:
            return self._summary
        return {"events_count": self.events, "favourite_recipes": [],
                "disliked_recipes": [], "favourite_dish_types": [],
                "favourite_cuisines": [], "disliked_ingredients": []}

    # то, что нужно меню настроек
    async def get_profile(self, session):
        return {"user": {"id": OWNER["user_id"], "login": "tg7", "has_password": False},
                "household": {"id": OWNER["household_id"], "name": "Моя семья",
                              "role": "owner"},
                "people": [], "appliances": [], "dietary_rules": [],
                "telegram_linked": True}

    async def has_password(self, user_id):
        return False


class _PlainRepo:
    """Репозиторий до приезда TZ-M8: методов taste_* нет вовсе."""

    async def get_profile(self, session):
        return {"user": {}, "household": {"name": "Моя семья"}, "people": [],
                "appliances": [], "dietary_rules": []}

    async def has_password(self, user_id):
        return False


class _BotRepo:
    async def context_for_user(self, user_id):
        return OWNER


def buttons(reply):
    keyboard = (reply.keyboard or {}).get("inline_keyboard", [])
    return [button for row in keyboard for button in row]


def datas(reply):
    return [button["callback_data"] for button in buttons(reply)]


class AvailabilityTests(unittest.TestCase):
    """Функции нет — нет ни кнопки, ни команды, ни трассировки."""

    def test_available_reflects_repository(self):
        self.assertFalse(taste.available(_PlainRepo()))
        self.assertTrue(taste.available(_TasteRepo()))

    def test_command_hidden_until_model_arrives(self):
        names = [name for name, _ in commands_for(_PlainRepo())]
        self.assertNotIn("taste", names)
        self.assertIn("taste", [name for name, _ in commands_for(_TasteRepo())])
        # остальной список не пострадал
        self.assertEqual(len(commands_for(_TasteRepo())), len(BOT_COMMANDS))

    def test_settings_menu_hides_taste_without_model(self):
        plain = run_async(settings.begin(_Dialogs(), _PlainRepo(), OWNER, USER_ID))
        self.assertNotIn(encode_callback("n", "ts", "cards"), datas(plain))
        ready = run_async(settings.begin(_Dialogs(), _TasteRepo(), OWNER, USER_ID))
        self.assertIn(encode_callback("n", "ts", "cards"), datas(ready))

    def test_taste_command_explains_instead_of_failing(self):
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "/taste", TODAY,
            app_repository=_PlainRepo(), dialogs=_Dialogs(),
        ))
        self.assertIn("пока не включены", reply.text)

    def test_stale_button_from_old_message_is_answered(self):
        """Кнопка могла остаться в сообщении, отправленном до отката M8."""
        result = run_async(handle_callback(
            _PlainRepo(), _BotRepo(), USER_ID, encode_callback("t", 101, "like"), TODAY,
            dialogs=_Dialogs(),
        ))
        self.assertIn("пока не включены", result.edit.text)


class CardsTests(unittest.TestCase):
    def test_first_card_shows_dish_and_cuisine(self):
        reply = run_async(taste.begin(_TasteRepo(), _Dialogs(), OWNER, USER_ID))
        self.assertIn("Харчо", reply.text)
        self.assertIn("Грузинская", reply.text)
        self.assertIn("Суп", reply.text)
        self.assertIn("осталось карточек: 3", reply.text)

    def test_card_offers_three_answers(self):
        reply = run_async(taste.begin(_TasteRepo(), _Dialogs(), OWNER, USER_ID))
        labels = [button["text"] for button in buttons(reply)]
        self.assertIn("👍 Нравится", labels)
        self.assertIn("👎 Не моё", labels)
        self.assertIn("⏭ Пропустить", labels)

    def test_answer_buttons_fit_the_callback_budget(self):
        reply = run_async(taste.begin(_TasteRepo(), _Dialogs(), OWNER, USER_ID))
        for data in datas(reply):
            self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_LIMIT)
        parsed = parse_callback(encode_callback("t", 999999, "like"))
        self.assertEqual(parsed, ("t", ["999999", "like"]))

    def test_like_records_event_from_telegram(self):
        repo, dialogs = _TasteRepo(), _Dialogs()
        run_async(taste.begin(repo, dialogs, OWNER, USER_ID))
        result = run_async(taste.answer(
            repo, dialogs, OWNER, USER_ID, ["101", "like"]
        ))
        self.assertEqual(repo.recorded, [(101, "onboarding_like", "telegram")])
        self.assertEqual(result.toast, "Запомнил.")
        # ответивший рецепт выпал из колоды
        self.assertNotIn("Харчо", result.edit.text)
        self.assertIn("Лобио", result.edit.text)

    def test_dislike_records_skip_event(self):
        repo, dialogs = _TasteRepo(), _Dialogs()
        run_async(taste.begin(repo, dialogs, OWNER, USER_ID))
        run_async(taste.answer(repo, dialogs, OWNER, USER_ID, ["101", "skip"]))
        self.assertEqual(repo.recorded, [(101, "onboarding_skip", "telegram")])

    def test_pass_records_nothing_but_moves_on(self):
        repo, dialogs = _TasteRepo(), _Dialogs()
        run_async(taste.begin(repo, dialogs, OWNER, USER_ID))
        result = run_async(taste.answer(
            repo, dialogs, OWNER, USER_ID, ["101", "pass"]
        ))
        self.assertEqual(repo.recorded, [])
        self.assertIn("Лобио", result.edit.text)
        self.assertEqual(dialogs.state.data["passed"], [101])
        self.assertEqual(dialogs.state.scene, taste.SCENE)

    def test_passed_card_does_not_come_back(self):
        repo, dialogs = _TasteRepo(), _Dialogs()
        run_async(taste.begin(repo, dialogs, OWNER, USER_ID))
        run_async(taste.answer(repo, dialogs, OWNER, USER_ID, ["101", "pass"]))
        result = run_async(taste.answer(
            repo, dialogs, OWNER, USER_ID, ["202", "pass"]
        ))
        self.assertEqual(dialogs.state.data["passed"], [101, 202])
        self.assertNotIn("Харчо", result.edit.text)
        self.assertIn("Оливье", result.edit.text)

    def test_viewer_cannot_teach_taste(self):
        repo, dialogs = _TasteRepo(), _Dialogs()
        result = run_async(taste.answer(
            repo, dialogs, VIEWER, USER_ID, ["101", "like"]
        ))
        self.assertTrue(result.show_alert)
        self.assertEqual(repo.recorded, [])

    def test_empty_deck_offers_summary(self):
        repo, dialogs = _TasteRepo(cards=[]), _Dialogs()
        reply = run_async(taste.begin(repo, dialogs, OWNER, USER_ID))
        self.assertIn("закончились", reply.text)
        self.assertIn(encode_callback("n", "ts", "sum"), datas(reply))

    def test_unknown_answer_is_not_recorded(self):
        repo, dialogs = _TasteRepo(), _Dialogs()
        result = run_async(taste.answer(
            repo, dialogs, OWNER, USER_ID, ["101", "wat"]
        ))
        self.assertEqual(repo.recorded, [])
        self.assertIsNone(result.edit)

    def test_card_fits_one_telegram_message(self):
        long_title = "Х" * 500
        repo = _TasteRepo(cards=[card(101, long_title)])
        reply = run_async(taste.begin(repo, _Dialogs(), OWNER, USER_ID))
        self.assertLessEqual(len(reply.text), TELEGRAM_LIMIT)


class SummaryTests(unittest.TestCase):
    def test_summary_names_liked_and_disliked(self):
        repo = _TasteRepo(events=27, summary={
            "events_count": 27,
            "favourite_recipes": [{"recipe_id": 101, "score": 0.6, "title": "Харчо"}],
            "disliked_recipes": [{"recipe_id": 5, "score": -0.4, "title": "Треска"}],
            "favourite_dish_types": [{"key": "soup", "score": 0.5, "events_count": 4}],
            "favourite_cuisines": [{"key": "georgian", "score": 0.4, "events_count": 3}],
            "disliked_ingredients": [{"key": "рыба", "score": -0.5, "events_count": 3}],
        })
        reply = run_async(taste.summary_reply(repo, OWNER))
        self.assertIn("Суп", reply.text)
        self.assertIn("Грузинская", reply.text)
        self.assertIn("рыба", reply.text)
        self.assertIn("Харчо", reply.text)
        self.assertIn("Треска", reply.text)
        self.assertIn("27", reply.text)

    def test_summary_without_events_invites_to_cards(self):
        reply = run_async(taste.summary_reply(_TasteRepo(events=0), OWNER))
        self.assertIn("ничего не знаю", reply.text)
        self.assertIn(encode_callback("n", "ts", "cards"), datas(reply))

    def test_summary_keeps_only_three_names_per_line(self):
        many = [{"key": code, "score": 0.5, "events_count": 2} for code in
                ("soup", "salad", "steak", "sandwich", "appetizer")]
        repo = _TasteRepo(events=9, summary={
            "events_count": 9, "favourite_recipes": [], "disliked_recipes": [],
            "favourite_dish_types": many, "favourite_cuisines": [],
            "disliked_ingredients": [],
        })
        reply = run_async(taste.summary_reply(repo, OWNER))
        self.assertIn("Суп, Салат, Стейк", reply.text)
        self.assertNotIn("Сэндвич", reply.text)

    def test_summary_with_events_but_no_signal_says_so(self):
        repo = _TasteRepo(events=2, summary={
            "events_count": 2, "favourite_recipes": [], "disliked_recipes": [],
            "favourite_dish_types": [], "favourite_cuisines": [],
            "disliked_ingredients": [],
        })
        reply = run_async(taste.summary_reply(repo, OWNER))
        self.assertIn("мало", reply.text)


class RoutingTests(unittest.TestCase):
    def test_command_opens_deck(self):
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "/taste", TODAY,
            app_repository=_TasteRepo(), dialogs=_Dialogs(),
        ))
        self.assertIn("Харчо", reply.text)

    def test_navigation_between_cards_and_summary(self):
        repo, dialogs = _TasteRepo(events=1), _Dialogs()
        to_summary = run_async(handle_callback(
            repo, _BotRepo(), USER_ID, encode_callback("n", "ts", "sum"), TODAY,
            dialogs=dialogs,
        ))
        self.assertIn("Вкусы семьи", to_summary.edit.text)
        back = run_async(handle_callback(
            repo, _BotRepo(), USER_ID, encode_callback("n", "ts", "cards"), TODAY,
            dialogs=dialogs,
        ))
        self.assertIn("Харчо", back.edit.text)

    def test_verb_t_reaches_the_scene(self):
        repo, dialogs = _TasteRepo(), _Dialogs()
        run_async(handle_callback(
            repo, _BotRepo(), USER_ID, encode_callback("t", 101, "like"), TODAY,
            dialogs=dialogs,
        ))
        self.assertEqual(repo.recorded, [(101, "onboarding_like", "telegram")])

    def test_free_text_returns_to_buttons(self):
        repo = _TasteRepo()
        dialogs = _Dialogs(DialogState(taste.SCENE, "card", {"passed": [101]}))
        reply = run_async(taste.handle_step(SceneContext(
            actor=Actor(user_id=USER_ID, chat_id=USER_ID),
            text="люблю супы",
            state=dialogs.state, bot_repository=_BotRepo(), app_repository=repo,
            dialogs=dialogs, today=TODAY, session=OWNER,
        )))
        self.assertIn("кнопкой", reply.text)
        self.assertIn("Лобио", reply.text)


if __name__ == "__main__":
    unittest.main()
