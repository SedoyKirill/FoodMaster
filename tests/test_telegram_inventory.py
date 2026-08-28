"""Запасы одной строкой и каталог «Ленты» (TZ-M7 T8, §5.8–5.9)."""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.telegram.callbacks import encode_callback, pack_uuid, parse_callback
from app.telegram.fsm import DialogState
from app.telegram.scenes import SceneContext, inventory, products
from app.telegram.service import handle_callback, handle_message

USER_ID = 7
TODAY = date(2026, 8, 28)
CONTEXT = {
    "user_id": str(uuid.uuid4()), "login": "tg7", "role": "owner",
    "household_id": str(uuid.uuid4()), "household_name": "Моя семья",
}


def run_async(coro):
    return asyncio.run(coro)


def lot(name="молоко", *, quantity="1", unit="l", expires="2026-09-05",
        storage="fridge", lot_id=None):
    return {
        "id": str(lot_id or uuid.uuid4()), "name": name,
        "quantity": Decimal(quantity), "unit_code": unit,
        "expires_on": expires, "storage_area": storage,
        "created_at": "2026-08-28T10:00:00",
    }


class _Dialogs:
    def __init__(self, state=None):
        self.state = state

    async def load(self, user_id):
        return self.state

    async def save(self, user_id, state):
        self.state = state

    async def clear(self, user_id):
        self.state = None


class _AppRepo:
    def __init__(self, lots=None, *, add_error=None, delete_error=None, deleted=True):
        self.lots = list(lots or [])
        self.add_error = add_error
        self.delete_error = delete_error
        self.deleted = deleted
        self.added = []
        self.removed = []

    async def list_inventory(self, session):
        return self.lots

    async def add_inventory(self, session, item):
        if self.add_error:
            raise self.add_error
        self.added.append(item)
        stored = {**item, "id": str(uuid.uuid4()), "created_at": "2026-08-28"}
        self.lots.append(stored)
        return stored

    async def delete_inventory(self, session, item_id):
        if self.delete_error:
            raise self.delete_error
        self.removed.append(item_id)
        self.lots = [entry for entry in self.lots if str(entry["id"]) != str(item_id)]
        return self.deleted


class _BotRepo:
    context = CONTEXT

    async def context_for_user(self, user_id):
        return CONTEXT

    async def latest_plan_meals(self, household_id):
        return []

    async def shopping_items(self, household_id):
        return []


def buttons(reply):
    return [b for row in reply.keyboard["inline_keyboard"] for b in row]


def ctx(text, state, app_repository, dialogs):
    return SceneContext(
        actor=type("A", (), {"user_id": USER_ID, "chat_id": USER_ID})(),
        text=text, state=state, bot_repository=_BotRepo(),
        app_repository=app_repository, dialogs=dialogs, today=TODAY,
        session=CONTEXT,
    )


class ParseLotTests(unittest.TestCase):
    """§5.8: не менее двадцати случаев разбора строки."""

    def parse(self, text):
        return inventory.parse_lot(text, TODAY)

    def test_name_quantity_unit_and_date(self) -> None:
        result = self.parse("молоко 1 л до 05.09")
        self.assertEqual(result.name, "молоко")
        self.assertEqual(result.quantity, Decimal("1"))
        self.assertEqual(result.unit_code, "l")
        self.assertEqual(result.expires_on, date(2026, 9, 5))
        self.assertEqual(result.storage_area, "fridge")
        self.assertEqual(result.missing, [])

    def test_bare_number_means_pieces(self) -> None:
        """Приёмка §9.3: «яйца 10» создаётся сразу, а не спрашивает единицу."""
        result = self.parse("яйца 10")
        self.assertEqual((result.name, result.quantity, result.unit_code),
                         ("яйца", Decimal("10"), "piece"))
        self.assertEqual(result.missing, [])

    def test_pieces_word(self) -> None:
        result = self.parse("яйца 10 шт")
        self.assertEqual((result.quantity, result.unit_code), (Decimal("10"), "piece"))

    def test_grams_and_storage(self) -> None:
        result = self.parse("курица 800 г морозилка")
        self.assertEqual(result.name, "курица")
        self.assertEqual((result.quantity, result.unit_code), (Decimal("800"), "g"))
        self.assertEqual(result.storage_area, "freezer")

    def test_only_name_asks_for_quantity(self) -> None:
        result = self.parse("сыр")
        self.assertEqual(result.name, "сыр")
        self.assertEqual(result.missing, ["quantity"])

    def test_empty_string_has_nothing(self) -> None:
        self.assertIn("name", self.parse("   ").missing)

    def test_kilograms(self) -> None:
        self.assertEqual(self.parse("картофель 2 кг").unit_code, "kg")

    def test_millilitres(self) -> None:
        self.assertEqual(self.parse("сливки 200 мл").unit_code, "ml")

    def test_decimal_with_comma(self) -> None:
        self.assertEqual(self.parse("масло 0,5 л").quantity, Decimal("0.5"))

    def test_decimal_with_dot(self) -> None:
        self.assertEqual(self.parse("масло 1.5 кг").quantity, Decimal("1.5"))

    def test_word_forms_of_units(self) -> None:
        self.assertEqual(self.parse("сахар 500 граммов").unit_code, "g")
        self.assertEqual(self.parse("вода 2 литра").unit_code, "l")
        self.assertEqual(self.parse("яблоки 5 штук").unit_code, "piece")

    def test_full_date_with_year(self) -> None:
        self.assertEqual(self.parse("кефир 1 л до 05.09.2027").expires_on,
                         date(2027, 9, 5))

    def test_date_without_preposition(self) -> None:
        self.assertEqual(self.parse("кефир 1 л 05.09").expires_on, date(2026, 9, 5))

    def test_relative_days(self) -> None:
        self.assertEqual(self.parse("хлеб 1 шт +3 дня").expires_on, date(2026, 8, 31))

    def test_relative_through_days(self) -> None:
        result = self.parse("хлеб 1 шт через 2 дня")
        self.assertEqual(result.expires_on, date(2026, 8, 30))
        # окончание слова не должно оставаться в названии
        self.assertEqual(result.name, "хлеб")

    def test_relative_days_leave_clean_name(self) -> None:
        for phrase in ("через 3 дня", "через 5 дней", "+2 суток", "через 2 недели"):
            self.assertEqual(self.parse(f"творог 200 г {phrase}").name, "творог")

    def test_relative_weeks(self) -> None:
        self.assertEqual(self.parse("сыр 300 г через 2 недели").expires_on,
                         date(2026, 9, 11))

    def test_worded_month(self) -> None:
        self.assertEqual(self.parse("творог 200 г до 5 сентября").expires_on,
                         date(2026, 9, 5))

    def test_worded_month_next_year(self) -> None:
        # январь этого года уже прошёл — значит следующий
        self.assertEqual(self.parse("варенье 1 л до 5 января").expires_on,
                         date(2027, 1, 5))

    def test_impossible_date_is_ignored(self) -> None:
        result = self.parse("молоко 1 л до 32.13")
        self.assertIsNone(result.expires_on)

    def test_storage_in_prepositional_case(self) -> None:
        self.assertEqual(self.parse("рыба 1 кг в морозилке").storage_area, "freezer")

    def test_pantry(self) -> None:
        self.assertEqual(self.parse("крупа 1 кг шкаф").storage_area, "pantry")

    def test_storage_defaults_to_fridge(self) -> None:
        self.assertEqual(self.parse("йогурт 4 шт").storage_area, "fridge")

    def test_multiword_name_survives(self) -> None:
        self.assertEqual(self.parse("сметана 20 процентов 1 шт").name.startswith("сметана"),
                         True)

    def test_everything_at_once(self) -> None:
        result = self.parse("филе индейки 1.2 кг морозилка до 12.09")
        self.assertEqual(result.name, "филе индейки")
        self.assertEqual((result.quantity, result.unit_code), (Decimal("1.2"), "kg"))
        self.assertEqual(result.storage_area, "freezer")
        self.assertEqual(result.expires_on, date(2026, 9, 12))

    def test_zero_quantity_is_not_a_quantity(self) -> None:
        self.assertIn("quantity", self.parse("соль 0").missing)

    def test_name_is_trimmed_to_limit(self) -> None:
        self.assertLessEqual(len(self.parse("х" * 200 + " 1 шт").name), 120)


class ListTests(unittest.TestCase):
    def test_badges_by_expiry(self) -> None:
        lots = [
            lot("просрочка", expires="2026-08-01"),
            lot("сегодня", expires="2026-08-28"),
            lot("скоро", expires="2026-08-30"),
            lot("нескоро", expires="2026-09-20"),
            lot("вечное", expires=None),
        ]
        text = inventory.list_reply(lots, TODAY).text
        self.assertIn("просрочен", text)
        self.assertIn("сегодня", text)
        self.assertIn("2 дн.", text)
        self.assertIn("до 20.09", text)
        self.assertIn("без срока", text)

    def test_expired_count_in_header(self) -> None:
        lots = [lot("а", expires="2026-08-01"), lot("б", expires="2026-08-02")]
        self.assertIn("Просрочено: 2", inventory.list_reply(lots, TODAY).text)

    def test_pagination_by_eight(self) -> None:
        lots = [lot(f"позиция {index}") for index in range(20)]
        reply = inventory.list_reply(lots, TODAY, 2)
        trash = [b for b in buttons(reply) if b["text"].startswith("🗑")]
        self.assertEqual(len(trash), 8)
        self.assertIn("Страница 2 из 3", reply.text)

    def test_empty_list_offers_presets(self) -> None:
        reply = inventory.list_reply([], TODAY)
        self.assertIn("Запасов пока нет", reply.text)
        self.assertIn("Молоко", [b["text"] for b in buttons(reply)])


class AddTests(unittest.TestCase):
    def test_line_is_stored(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(inventory.SCENE, "line", {}))
        reply = run_async(inventory.add_line(
            app_repository, dialogs, CONTEXT, USER_ID, "молоко 1 л до 05.09", TODAY
        ))
        self.assertEqual(app_repository.added[0]["name"], "молоко")
        self.assertEqual(app_repository.added[0]["unit_code"], "l")
        self.assertIn("Добавил", reply.text)

    def test_missing_quantity_asks(self) -> None:
        dialogs = _Dialogs(DialogState(inventory.SCENE, "line", {}))
        app_repository = _AppRepo()
        reply = run_async(inventory.add_line(
            app_repository, dialogs, CONTEXT, USER_ID, "сыр", TODAY
        ))
        self.assertEqual(app_repository.added, [])
        self.assertEqual(dialogs.state.step, "quantity")
        self.assertIn("сколько", reply.text.lower())

    def test_answering_quantity_completes_the_lot(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(
            inventory.SCENE, "quantity", {"draft": {"name": "сыр", "storage_area": "fridge"}}
        ))
        run_async(inventory.handle_step(ctx("300 г", dialogs.state, app_repository, dialogs)))
        self.assertEqual(app_repository.added[0]["name"], "сыр")
        self.assertEqual(app_repository.added[0]["unit_code"], "g")

    def test_unit_button_then_number(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(
            inventory.SCENE, "quantity", {"draft": {"name": "сыр", "storage_area": "fridge"}}
        ))
        result = run_async(inventory.handle_filter(
            app_repository, dialogs, CONTEXT, USER_ID, ["in", "u", "g"], TODAY
        ))
        self.assertIn("сколько г", result.edit.text)
        run_async(inventory.handle_step(ctx("250", dialogs.state, app_repository, dialogs)))
        self.assertEqual(app_repository.added[0]["unit_code"], "g")

    def test_preset_starts_the_form(self) -> None:
        dialogs = _Dialogs(DialogState(inventory.SCENE, "line", {}))
        result = run_async(inventory.handle_filter(
            _AppRepo(), dialogs, CONTEXT, USER_ID, ["in", "p", "0"], TODAY
        ))
        self.assertIn("Молоко", result.edit.text)
        self.assertEqual(dialogs.state.step, "quantity")

    def test_expired_date_asks_for_confirmation(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(inventory.SCENE, "line", {}))
        reply = run_async(inventory.add_line(
            app_repository, dialogs, CONTEXT, USER_ID, "кефир 1 л до 01.08", TODAY
        ))
        self.assertEqual(app_repository.added, [])
        self.assertIn("просрочен", reply.text)
        self.assertEqual(dialogs.state.step, "expired")

    def test_expired_confirmation_stores_it(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(inventory.SCENE, "line", {}))
        run_async(inventory.add_line(
            app_repository, dialogs, CONTEXT, USER_ID, "кефир 1 л до 01.08", TODAY
        ))
        run_async(inventory.confirm_expired(
            app_repository, dialogs, CONTEXT, USER_ID, TODAY
        ))
        self.assertEqual(app_repository.added[0]["name"], "кефир")

    def test_viewer_cannot_add(self) -> None:
        app_repository = _AppRepo(add_error=PermissionError("Режим просмотра"))
        reply = run_async(inventory.add_line(
            app_repository, _Dialogs(DialogState(inventory.SCENE, "line", {})),
            CONTEXT, USER_ID, "молоко 1 л", TODAY,
        ))
        self.assertIn("Режим просмотра", reply.text)

    def test_unparsable_line_explains(self) -> None:
        reply = run_async(inventory.add_line(
            _AppRepo(), _Dialogs(DialogState(inventory.SCENE, "line", {})),
            CONTEXT, USER_ID, "   ", TODAY,
        ))
        self.assertIn("Напишите одной строкой", reply.text)


class DeleteTests(unittest.TestCase):
    def test_delete_offers_undo(self) -> None:
        target = lot("молоко", lot_id=uuid.uuid4())
        app_repository = _AppRepo([target])
        dialogs = _Dialogs(DialogState(inventory.SCENE, "line", {}))
        result = run_async(inventory.delete(
            app_repository, dialogs, CONTEXT, USER_ID, pack_uuid(target["id"]), TODAY
        ))
        self.assertEqual(len(app_repository.removed), 1)
        self.assertIn("↩ Вернуть", [b["text"] for b in buttons(result.edit)])
        self.assertEqual(dialogs.state.data["undo"]["name"], "молоко")

    def test_undo_restores_the_lot(self) -> None:
        target = lot("молоко", lot_id=uuid.uuid4())
        app_repository = _AppRepo([target])
        dialogs = _Dialogs(DialogState(inventory.SCENE, "line", {}))
        run_async(inventory.delete(
            app_repository, dialogs, CONTEXT, USER_ID, pack_uuid(target["id"]), TODAY
        ))
        run_async(inventory.undo_delete(app_repository, dialogs, CONTEXT, USER_ID, TODAY))
        self.assertEqual(app_repository.added[0]["name"], "молоко")
        self.assertEqual(app_repository.added[0]["unit_code"], "l")

    def test_undo_without_history_says_so(self) -> None:
        result = run_async(inventory.undo_delete(
            _AppRepo(), _Dialogs(DialogState(inventory.SCENE, "line", {})),
            CONTEXT, USER_ID, TODAY,
        ))
        self.assertTrue(result.show_alert)

    def test_deleting_missing_lot(self) -> None:
        result = run_async(inventory.delete(
            _AppRepo([]), _Dialogs(), CONTEXT, USER_ID, pack_uuid(uuid.uuid4()), TODAY
        ))
        self.assertTrue(result.show_alert)

    def test_viewer_cannot_delete(self) -> None:
        target = lot("молоко", lot_id=uuid.uuid4())
        app_repository = _AppRepo(
            [target], delete_error=PermissionError("Режим просмотра")
        )
        result = run_async(inventory.delete(
            app_repository, _Dialogs(), CONTEXT, USER_ID, pack_uuid(target["id"]), TODAY
        ))
        self.assertIn("Режим просмотра", result.toast)


class _ProductRepo:
    def __init__(self, items=None, total=None, categories=None):
        self.items = items if items is not None else [{
            "id": index, "name": f"Товар {index}", "brand": "ЛЕНТА",
            "pack_text": "1 кг", "regular_price_kop": 20000,
            "loyalty_price_kop": 18000, "promo_price_kop": 15000,
            "discount_percent": 25, "effective_price_kop": 15000,
            "kcal_100": 120, "protein_100": 3, "fat_100": 5, "carb_100": 15,
            "url": f"https://lenta.com/product/{index}",
        } for index in range(1, 9)]
        self.total = total if total is not None else len(self.items)
        self.categories = categories or [
            {"category_slug": "syry-2", "product_count": 120},
            {"category_slug": "napitki-4", "product_count": 90},
        ]
        self.calls = []

    async def list_products(self, **kwargs):
        self.calls.append(kwargs)
        return {"items": self.items, "total": self.total}

    async def product_categories(self):
        return self.categories


class ProductsTests(unittest.TestCase):
    def test_search_lists_products_with_price(self) -> None:
        reply = run_async(products.results_reply(_ProductRepo(), {}))
        self.assertIn("Нашёл 8", reply.text)
        self.assertIn("150 ₽", buttons(reply)[0]["text"])

    def test_free_text_is_the_query(self) -> None:
        repository = _ProductRepo()
        dialogs = _Dialogs(DialogState(products.SCENE, "query", {}))
        run_async(products.handle_step(SceneContext(
            actor=type("A", (), {"user_id": USER_ID, "chat_id": USER_ID})(),
            text="молоко", state=dialogs.state, bot_repository=_BotRepo(),
            app_repository=repository, dialogs=dialogs, today=TODAY, session=CONTEXT,
        )))
        self.assertEqual(repository.calls[0]["search"], "молоко")

    def test_discount_toggle(self) -> None:
        dialogs = _Dialogs(DialogState(products.SCENE, "query", {}))
        repository = _ProductRepo()
        run_async(products.handle_filter(repository, dialogs, USER_ID, ["pr", "d"]))
        self.assertTrue(dialogs.state.data["discount"])
        self.assertTrue(repository.calls[-1]["discount_only"])

    def test_sort_choices_and_applying(self) -> None:
        dialogs = _Dialogs(DialogState(products.SCENE, "query", {}))
        repository = _ProductRepo()
        choices = run_async(products.handle_filter(repository, dialogs, USER_ID, ["pr", "s"]))
        self.assertIn("☐ Сначала дешевле", [b["text"] for b in buttons(choices.edit)])
        run_async(products.handle_filter(repository, dialogs, USER_ID, ["pr", "s", "a"]))
        self.assertEqual(repository.calls[-1]["sort"], "price_asc")

    def test_category_choices_use_labels(self) -> None:
        dialogs = _Dialogs(DialogState(products.SCENE, "query", {}))
        result = run_async(products.handle_filter(
            _ProductRepo(), dialogs, USER_ID, ["pr", "k"]
        ))
        labels = [b["text"] for b in buttons(result.edit)]
        self.assertTrue(any("Сыры" in label for label in labels))
        self.assertIn("Все разделы", labels)

    def test_card_shows_three_prices_and_nutrition(self) -> None:
        product = _ProductRepo().items[0]
        text = products.card_reply(product).text
        self.assertIn("Обычная цена: 200 ₽", text)
        self.assertIn("По карте: 180 ₽", text)
        self.assertIn("Акция: 150 ₽ (−25 %)", text)
        self.assertIn("ккал 120", text)
        self.assertIn("https://lenta.com/product/1", text)

    def test_card_button_opens_it(self) -> None:
        dialogs = _Dialogs(DialogState(products.SCENE, "query", {}))
        result = run_async(products.handle_navigation(
            _ProductRepo(), dialogs, USER_ID, ["pr", "c", "3"]
        ))
        self.assertIn("Товар 3", result.edit.text)

    def test_missing_product_says_so(self) -> None:
        result = run_async(products.handle_navigation(
            _ProductRepo(), _Dialogs(DialogState(products.SCENE, "query", {})),
            USER_ID, ["pr", "c", "999"],
        ))
        self.assertTrue(result.show_alert)


class DispatchTests(unittest.TestCase):
    def test_inventory_command_opens_scene(self) -> None:
        dialogs = _Dialogs()
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "🧊 Запасы", TODAY,
            app_repository=_AppRepo([lot()]), dialogs=dialogs,
        ))
        self.assertEqual(dialogs.state.scene, inventory.SCENE)
        self.assertIn("Запасы", reply.text)

    def test_products_command_opens_scene(self) -> None:
        dialogs = _Dialogs()
        run_async(handle_message(
            _BotRepo(), USER_ID, "/products", TODAY,
            app_repository=_ProductRepo(), dialogs=dialogs,
        ))
        self.assertEqual(dialogs.state.scene, products.SCENE)

    def test_delete_button_goes_through_dispatch(self) -> None:
        target = lot("молоко", lot_id=uuid.uuid4())
        app_repository = _AppRepo([target])
        run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID,
            encode_callback("i", pack_uuid(target["id"])), TODAY,
            dialogs=_Dialogs(DialogState(inventory.SCENE, "line", {})),
        ))
        self.assertEqual(len(app_repository.removed), 1)

    def test_inventory_page_button(self) -> None:
        lots = [lot(f"позиция {index}") for index in range(20)]
        result = run_async(handle_callback(
            _AppRepo(lots), _BotRepo(), USER_ID, encode_callback("p", "in", 3), TODAY,
            dialogs=_Dialogs(),
        ))
        self.assertIn("Страница 3 из 3", result.edit.text)

    def test_all_buttons_fit_in_64_bytes(self) -> None:
        lots = [lot(f"позиция {index}") for index in range(20)]
        screens = [
            inventory.list_reply(lots, TODAY, 2),
            inventory.list_reply([], TODAY),
            run_async(products.results_reply(_ProductRepo(total=40), {"page": 2})),
            products.card_reply(_ProductRepo().items[0]),
        ]
        for reply in screens:
            for button in buttons(reply):
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
