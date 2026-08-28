"""Inline-слой Telegram-бота: кодек callback_data, клавиатуры, обработчики."""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.telegram.service import (
    CallbackReply, Reply, STALE_TEXT, alternatives_keyboard, encode_callback,
    format_recipe, handle_callback, pack_uuid, parse_callback, shopping_keyboard,
    split_for_telegram, today_keyboard, unpack_uuid,
)

TODAY = date(2026, 8, 18)
PLAN = uuid.uuid4()
MEAL = uuid.uuid4()
ITEM = uuid.uuid4()


class CodecTests(unittest.TestCase):
    def test_uuid_roundtrip(self) -> None:
        packed = pack_uuid(PLAN)
        self.assertEqual(len(packed), 22)
        self.assertEqual(unpack_uuid(packed), PLAN)

    def test_unpack_garbage_returns_none(self) -> None:
        self.assertIsNone(unpack_uuid("не-uuid!"))
        self.assertIsNone(unpack_uuid(""))

    def test_encode_fits_64_bytes(self) -> None:
        data = encode_callback("v", pack_uuid(PLAN), pack_uuid(MEAL), 9_999_999)
        self.assertLessEqual(len(data.encode("utf-8")), 64)

    def test_parse_rejects_unknown_verbs(self) -> None:
        self.assertIsNone(parse_callback("z|abc"))
        self.assertIsNone(parse_callback("мусор"))
        self.assertEqual(parse_callback("s|a|b"), ("s", ["a", "b"]))


class KeyboardTests(unittest.TestCase):
    def test_shopping_keyboard_marks_and_parses_back(self) -> None:
        items = [
            {"id": str(ITEM), "normalized_name": "молоко", "buy_quantity": "930",
             "unit_code": "ml", "estimated_cost_kop": 9900, "purchased_at": None},
            {"id": str(uuid.uuid4()), "normalized_name": "оченьдлинноеназваниепродукта" * 4,
             "buy_quantity": "1", "unit_code": "piece", "estimated_cost_kop": None,
             "purchased_at": "2026-08-18"},
            {"id": str(uuid.uuid4()), "normalized_name": "не покупать",
             "buy_quantity": "0", "unit_code": "g", "estimated_cost_kop": None,
             "purchased_at": None},
        ]
        keyboard = shopping_keyboard(PLAN, items)
        rows = keyboard["inline_keyboard"]
        self.assertEqual(len(rows), 2)  # buy_quantity=0 не показывается
        self.assertTrue(rows[0][0]["text"].startswith("☐"))
        self.assertTrue(rows[1][0]["text"].startswith("✅"))  # купленное — можно снять
        self.assertLessEqual(len(rows[1][0]["text"]), 60)
        verb, parts = parse_callback(rows[0][0]["callback_data"])
        self.assertEqual(verb, "s")
        self.assertEqual(unpack_uuid(parts[1]), ITEM)

    def test_today_keyboard_row_per_meal(self) -> None:
        meals = [
            {"id": str(MEAL), "meal_type": "breakfast"},
            {"id": str(uuid.uuid4()), "meal_type": "dinner"},
        ]
        rows = today_keyboard(PLAN, meals)["inline_keyboard"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(parse_callback(rows[0][0]["callback_data"])[0], "r")
        self.assertEqual(parse_callback(rows[0][1]["callback_data"])[0], "x")

    def test_alternatives_keyboard_caps_and_cancels(self) -> None:
        alternatives = [
            {"recipe_id": index, "title": f"Блюдо {index}", "source_page_start": 10 + index,
             "draft": index == 2}
            for index in range(1, 5)
        ]
        rows = alternatives_keyboard(PLAN, MEAL, alternatives)["inline_keyboard"]
        self.assertEqual(len(rows), 4)  # 3 варианта + отмена
        self.assertIn("(черновик)", rows[1][0]["text"])
        self.assertEqual(parse_callback(rows[3][0]["callback_data"])[0], "c")


class FormatRecipeTests(unittest.TestCase):
    def test_recipe_card_uses_meal_kbju_and_numbers_steps(self) -> None:
        detail = {
            "title": "Блины на кефире (черновик)",
            "source_page_start": 33, "source_servings_min": 2,
            "time_total_minutes": 40,
            "ingredients": [
                {"raw_text": "Кефир - 500 мл", "is_to_taste": False},
                {"raw_text": "Соль", "is_to_taste": True},
            ],
            "steps": [
                {"position": 1, "instruction": "Смешать."},
                {"position": 2, "instruction": "Жарить."},
            ],
        }
        meal = {"estimated_kcal": 943, "estimated_protein": 30,
                "estimated_fat": 29, "estimated_carb": 110}
        text = format_recipe(detail, meal)
        self.assertIn("стр. 33", text)
        self.assertIn("≈943 ккал", text)
        self.assertIn("Б/Ж/У 30/29/110 г", text)
        self.assertIn("Соль — по вкусу", text)
        self.assertIn("\n\n1. Смешать.", text)
        self.assertIn("\n\n2. Жарить.", text)


class SplitTests(unittest.TestCase):
    def test_short_text_single_chunk(self) -> None:
        self.assertEqual(split_for_telegram("привет"), ["привет"])

    def test_splits_on_paragraphs_and_preserves_content(self) -> None:
        paragraphs = [f"шаг {index}: " + "х" * 300 for index in range(30)]
        text = "\n\n".join(paragraphs)
        chunks = split_for_telegram(text, limit=1000)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertTrue(all(chunk.strip() for chunk in chunks))
        for paragraph in paragraphs:
            self.assertTrue(any(paragraph in chunk for chunk in chunks))

    def test_monster_line_hard_cut(self) -> None:
        chunks = split_for_telegram("x" * 5000, limit=1000)
        self.assertEqual(len(chunks), 5)
        self.assertEqual("".join(chunks), "x" * 5000)


class _StubBotRepository:
    def __init__(self, context):
        self.context = context
        self.context_calls: list[int] = []

    async def context_for_user(self, user_id):
        self.context_calls.append(user_id)
        return self.context

    async def latest_plan_meals(self, household_id):
        return []

    async def shopping_items(self, household_id):
        return []


class _StubAppRepository:
    """Канированные ответы AppRepository + журнал вызовов."""

    def __init__(self, plan=None, latest=None, replace_results=None,
                 mark_result=None, detail=None):
        self.plan = plan
        self.latest = latest if latest is not None else plan
        self.replace_results = replace_results or []
        self.mark_result = mark_result
        self.detail = detail
        self.calls: list[tuple] = []

    async def get_plan(self, session, plan_id):
        self.calls.append(("get_plan", str(plan_id)))
        return self.plan

    async def latest_plan(self, session):
        self.calls.append(("latest_plan",))
        return self.latest

    async def mark_purchased(self, session, plan_id, item_id, purchased):
        self.calls.append(("mark_purchased", str(item_id), purchased))
        return self.mark_result

    async def replace_meal(self, session, plan_id, meal_id, recipe_id=None):
        self.calls.append(("replace_meal", str(meal_id), recipe_id))
        result = self.replace_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def recipe_detail(self, recipe_id, household_id=None):
        self.calls.append(("recipe_detail", recipe_id, str(household_id)))
        return self.detail


CONTEXT = {"household_id": "h-1", "user_id": "u-1", "role": "owner", "login": "vanya"}


def _plan_payload():
    return {
        "id": str(PLAN),
        "meals": [{
            "id": str(MEAL), "meal_date": TODAY, "meal_type": "dinner",
            "recipe_id": 7, "title": "Суп", "estimated_kcal": 500,
            "estimated_protein": None, "estimated_fat": None, "estimated_carb": None,
        }],
        "shopping": [{
            "id": str(ITEM), "normalized_name": "молоко", "buy_quantity": "930",
            "unit_code": "ml", "estimated_cost_kop": 9900, "purchased_at": None,
        }],
    }


def _call(app, data, bot_repository=None):
    return asyncio.run(handle_callback(
        app, bot_repository or _StubBotRepository(CONTEXT), 42, data, TODAY
    ))


class HandleCallbackTests(unittest.TestCase):
    def test_toggle_inverts_current_state(self) -> None:
        app = _StubAppRepository(
            plan=_plan_payload(),
            mark_result={"id": str(ITEM), "normalized_name": "молоко",
                         "purchased_at": "2026-08-18T12:00:00"},
        )
        data = encode_callback("s", pack_uuid(PLAN), pack_uuid(ITEM))
        result = _call(app, data)
        self.assertIn(("mark_purchased", str(ITEM), True), app.calls)
        self.assertIn("Куплено", result.toast)
        self.assertIsNotNone(result.edit)
        self.assertIn("Всё куплено", result.edit.text)

    def test_toggle_stale_item_strips_keyboard(self) -> None:
        app = _StubAppRepository(plan=_plan_payload(), mark_result=None)
        data = encode_callback("s", pack_uuid(PLAN), pack_uuid(ITEM))
        result = _call(app, data)
        self.assertTrue(result.show_alert)
        self.assertEqual(result.edit.text, STALE_TEXT)
        self.assertIsNone(result.edit.keyboard)

    def test_recipe_requires_meal_from_family_plan(self) -> None:
        app = _StubAppRepository(plan=_plan_payload(), detail={"title": "Суп", "steps": []})
        foreign_meal = uuid.uuid4()
        data = encode_callback("r", pack_uuid(PLAN), pack_uuid(foreign_meal))
        result = _call(app, data)
        self.assertTrue(result.show_alert)
        self.assertNotIn(
            "recipe_detail", [call[0] for call in app.calls],
            "recipe_detail не должен вызываться для чужого meal_id",
        )

    def test_recipe_sends_card(self) -> None:
        app = _StubAppRepository(
            plan=_plan_payload(),
            detail={"title": "Суп", "source_page_start": 5, "ingredients": [], "steps": []},
        )
        data = encode_callback("r", pack_uuid(PLAN), pack_uuid(MEAL))
        result = _call(app, data)
        self.assertEqual(len(result.sends), 1)
        self.assertIn("Суп", result.sends[0].text)
        self.assertIn(("recipe_detail", 7, "h-1"), app.calls)

    def test_alternatives_flow(self) -> None:
        app = _StubAppRepository(
            plan=_plan_payload(),
            replace_results=[{"alternatives": [
                {"recipe_id": 8, "title": "Плов", "source_page_start": 84, "draft": False},
            ]}],
        )
        data = encode_callback("x", pack_uuid(PLAN), pack_uuid(MEAL))
        result = _call(app, data)
        self.assertIn("Чем заменить", result.edit.text)
        rows = result.edit.keyboard["inline_keyboard"]
        self.assertEqual(len(rows), 2)  # 1 вариант + отмена

    def test_alternatives_reject_old_plan(self) -> None:
        stale_latest = dict(_plan_payload(), id=str(uuid.uuid4()))
        app = _StubAppRepository(plan=_plan_payload(), latest=stale_latest)
        data = encode_callback("x", pack_uuid(PLAN), pack_uuid(MEAL))
        result = _call(app, data)
        self.assertTrue(result.show_alert)
        self.assertNotIn("replace_meal", [call[0] for call in app.calls])

    def test_apply_replacement_sends_fresh_menu(self) -> None:
        new_plan = _plan_payload()
        new_plan["meals"][0]["title"] = "Плов"
        app = _StubAppRepository(plan=_plan_payload(), replace_results=[new_plan])
        data = encode_callback("v", pack_uuid(PLAN), pack_uuid(MEAL), 8)
        result = _call(app, data)
        self.assertIn("Плов", result.edit.text)
        self.assertEqual(len(result.sends), 1)
        self.assertIsNotNone(result.sends[0].keyboard)

    def test_apply_replacement_value_error_shown(self) -> None:
        app = _StubAppRepository(
            plan=_plan_payload(),
            replace_results=[ValueError("Этот рецепт нельзя поставить в выбранный слот")],
        )
        data = encode_callback("v", pack_uuid(PLAN), pack_uuid(MEAL), 8)
        result = _call(app, data)
        self.assertIn("нельзя поставить", result.edit.text)

    def test_viewer_permission_error_becomes_alert(self) -> None:
        app = _StubAppRepository(plan=_plan_payload())

        async def deny(session, plan_id, item_id, purchased):
            raise PermissionError("Режим просмотра не позволяет менять план")

        app.mark_purchased = deny
        data = encode_callback("s", pack_uuid(PLAN), pack_uuid(ITEM))
        result = _call(app, data)
        self.assertTrue(result.show_alert)
        self.assertIn("Режим просмотра", result.toast)

    def test_unlinked_chat_gets_alert(self) -> None:
        app = _StubAppRepository()
        data = encode_callback("c", pack_uuid(PLAN))
        result = _call(app, data, bot_repository=_StubBotRepository(None))
        self.assertTrue(result.show_alert)

    def test_garbage_callback_is_polite(self) -> None:
        result = _call(_StubAppRepository(), "мусор|данные")
        self.assertEqual(result.toast, "Не понял кнопку.")


class _FakeClient:
    """Журналирующий транспорт для BotApp."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append(("send", text))
        return 100 + len(self.calls)

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.calls.append(("edit", message_id, text))

    async def answer_callback_query(self, callback_id, text="", show_alert=False):
        self.calls.append(("ack", callback_id, text))

    async def send_chat_action(self, chat_id, action="typing"):
        self.calls.append(("action", action))

    def count(self, kind):
        return sum(1 for call in self.calls if call[0] == kind)


class BotAppTests(unittest.TestCase):
    def _make_app(self, app_repository):
        from app.telegram.bot import BotApp

        client = _FakeClient()
        return BotApp(client, _StubBotRepository(CONTEXT), app_repository), client

    def _callback_update(self, data, update_id=1, chat_type="private"):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb-{update_id}", "data": data,
                "from": {"id": 42},
                "message": {"message_id": 55, "chat": {"id": 42, "type": chat_type}},
            },
        }

    def _text_update(self, text, update_id=1, chat_type="private", chat_id=42, user_id=42):
        return {
            "update_id": update_id,
            "message": {
                "message_id": 7, "text": text, "from": {"id": user_id},
                "chat": {"id": chat_id, "type": chat_type},
            },
        }

    def test_light_callback_acks_exactly_once(self) -> None:
        app_repository = _StubAppRepository(plan=_plan_payload())
        bot, client = self._make_app(app_repository)
        update = self._callback_update(encode_callback("c", pack_uuid(PLAN)))
        asyncio.run(bot.process_updates([update]))
        self.assertEqual(client.count("ack"), 1)
        self.assertEqual(client.count("edit"), 1)

    def test_heavy_callback_placeholder_before_completion(self) -> None:
        gate = asyncio.Event()
        release_seen: dict = {}

        class SlowRepo(_StubAppRepository):
            async def replace_meal(self, session, plan_id, meal_id, recipe_id=None):
                await gate.wait()
                release_seen["done"] = True
                return {"alternatives": []}

        app_repository = SlowRepo(plan=_plan_payload())
        bot, client = self._make_app(app_repository)
        update = self._callback_update(encode_callback("x", pack_uuid(PLAN), pack_uuid(MEAL)))

        async def scenario():
            await bot.process_updates([update])
            # конвейер вернулся, replace ещё висит на gate — доказательство create_task
            self.assertNotIn("done", release_seen)
            self.assertEqual(client.count("ack"), 1)
            self.assertTrue(any(call[0] == "send" and "⏳" in call[1] for call in client.calls))
            gate.set()
            await asyncio.gather(*bot.tasks)

        asyncio.run(scenario())
        self.assertIn("done", release_seen)

    def test_double_click_same_slot_rejected(self) -> None:
        gate = asyncio.Event()

        class SlowRepo(_StubAppRepository):
            def __init__(self):
                super().__init__(plan=_plan_payload())
                self.replace_calls = 0

            async def replace_meal(self, session, plan_id, meal_id, recipe_id=None):
                self.replace_calls += 1
                await gate.wait()
                return {"alternatives": []}

        app_repository = SlowRepo()
        bot, client = self._make_app(app_repository)
        data = encode_callback("x", pack_uuid(PLAN), pack_uuid(MEAL))

        async def scenario():
            await bot.process_updates([self._callback_update(data, 1)])
            await bot.process_updates([self._callback_update(data, 2)])
            self.assertTrue(any("Уже работаю" in str(call) for call in client.calls))
            gate.set()
            await asyncio.gather(*bot.tasks)

        asyncio.run(scenario())
        # replace_meal вызван один раз — второй клик отбит по in_flight
        self.assertEqual(app_repository.replace_calls, 1)

    def test_heavy_task_exception_keeps_loop_alive(self) -> None:
        class BoomRepo(_StubAppRepository):
            async def replace_meal(self, session, plan_id, meal_id, recipe_id=None):
                raise RuntimeError("boom")

        app_repository = BoomRepo(plan=_plan_payload())
        bot, client = self._make_app(app_repository)
        update = self._callback_update(encode_callback("x", pack_uuid(PLAN), pack_uuid(MEAL)))

        async def scenario():
            await bot.process_updates([update])
            await asyncio.gather(*bot.tasks, return_exceptions=True)

        asyncio.run(scenario())
        self.assertTrue(
            any(call[0] == "edit" and "Не получилось" in call[2] for call in client.calls)
        )

    def test_callback_without_message_just_acks(self) -> None:
        app_repository = _StubAppRepository(plan=_plan_payload())
        bot, client = self._make_app(app_repository)
        # сообщение недоступно (слишком старое) — но кто нажал, Telegram знает
        update = {
            "update_id": 9,
            "callback_query": {"id": "cb-9", "data": "c|xx", "from": {"id": 42}},
        }
        offset = asyncio.run(bot.process_updates([update]))
        self.assertEqual(offset, 10)
        self.assertEqual(client.count("ack"), 1)

    # --- только личные чаты (TZ-M7 §3.1 / А2) ---------------------------------

    def test_group_message_gets_single_refusal(self) -> None:
        from app.telegram.bot import GROUP_REFUSAL

        bot, client = self._make_app(_StubAppRepository(plan=_plan_payload()))
        asyncio.run(bot.process_updates([
            self._text_update("🍽 Сегодня", 1, chat_type="supergroup", chat_id=-100),
            self._text_update("🛒 Покупки", 2, chat_type="supergroup", chat_id=-100),
        ]))
        refusals = [call for call in client.calls if call[0] == "send" and call[1] == GROUP_REFUSAL]
        self.assertEqual(len(refusals), 1)  # одна фраза на чат, а не на сообщение
        self.assertEqual(client.count("send"), 1)  # меню группе не показывали

    def test_group_callback_only_alerts(self) -> None:
        from app.telegram.bot import GROUP_REFUSAL

        app_repository = _StubAppRepository(plan=_plan_payload())
        bot, client = self._make_app(app_repository)
        data = encode_callback("s", pack_uuid(PLAN), pack_uuid(ITEM))
        asyncio.run(bot.process_updates([
            self._callback_update(data, 1, chat_type="group")
        ]))
        self.assertTrue(any(call[0] == "ack" and call[2] == GROUP_REFUSAL for call in client.calls))
        self.assertEqual(app_repository.calls, [])  # к данным семьи не ходили

    def test_private_message_still_works(self) -> None:
        bot, client = self._make_app(_StubAppRepository(plan=_plan_payload()))
        asyncio.run(bot.process_updates([self._text_update("/help", 1)]))
        self.assertEqual(client.count("send"), 1)

    # --- личность и лимиты (TZ-M7 §3.1, §3.5) ---------------------------------

    def test_identity_comes_from_sender_not_chat(self) -> None:
        bot_repository = _StubBotRepository(CONTEXT)
        bot = self._make_app(_StubAppRepository(plan=_plan_payload()))[0]
        bot.bot_repository = bot_repository
        asyncio.run(bot.process_updates([
            self._text_update("🛒 Покупки", 1, chat_id=42, user_id=7)
        ]))
        # семью ищем по нажавшему, а не по чату — иначе в группе доступ у всех
        self.assertEqual(bot_repository.context_calls, [7])

    def test_flood_gets_one_refusal(self) -> None:
        from fakes import StubClock

        from app.telegram.bot import BotApp
        from app.telegram.router import TOO_FAST_TEXT, Router

        clock = StubClock()
        client = _FakeClient()
        bot = BotApp(
            client, _StubBotRepository(CONTEXT), _StubAppRepository(plan=_plan_payload()),
            router=Router(clock=clock),
        )
        updates = [self._text_update("/help", index) for index in range(1, 26)]
        asyncio.run(bot.process_updates(updates))
        answered = [call for call in client.calls if call[0] == "send"]
        refusals = [call for call in answered if call[1] == TOO_FAST_TEXT]
        self.assertEqual(len(answered) - len(refusals), 20)  # 20 сообщений в минуту
        self.assertEqual(len(refusals), 1)  # об отказе говорим один раз

    # --- «⏳» не висит дольше срока (приёмка §9.9) -----------------------------

    def test_heavy_timeout_replaces_placeholder(self) -> None:
        class HangingRepo(_StubAppRepository):
            async def replace_meal(self, session, plan_id, meal_id, recipe_id=None):
                await asyncio.Event().wait()  # никогда не завершится

        bot, client = self._make_app(HangingRepo(plan=_plan_payload()))
        bot.heavy_timeout = 0.01
        update = self._callback_update(encode_callback("x", pack_uuid(PLAN), pack_uuid(MEAL)))

        async def scenario():
            await bot.process_updates([update])
            await asyncio.gather(*bot.tasks, return_exceptions=True)

        asyncio.run(scenario())
        self.assertTrue(
            any(call[0] == "edit" and "Не успел" in call[2] for call in client.calls)
        )
        self.assertEqual(bot.in_flight, set())


if __name__ == "__main__":
    unittest.main()
