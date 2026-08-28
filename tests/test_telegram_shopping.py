"""Покупки по разделам магазина (TZ-M7 T6, §5.6)."""

import asyncio
import os
import re
import sys
import unittest
import uuid
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.telegram.callbacks import encode_callback, pack_uuid, parse_callback
from app.telegram.scenes import shopping
from app.telegram.dispatch import handle_callback, handle_message
from app.web.categories import CATEGORY_LABELS, category_label

USER_ID = 7
TODAY = __import__("datetime").date(2026, 8, 28)
PLAN_ID = uuid.uuid4()
CONTEXT = {
    "user_id": str(uuid.uuid4()), "login": "tg7", "role": "owner",
    "household_id": str(uuid.uuid4()), "household_name": "Моя семья",
}
DAIRY = "molochnye-produkty-yajjco-3"
MEAT = "myaso-i-ptica-136"


def run_async(coro):
    return asyncio.run(coro)


def item(name, *, slug=DAIRY, buy=1, cost=17800, purchased=False, packs=2,
         to_taste=False, home=None, product="Сметана 20 % «Простоквашино»",
         url="https://lenta.com/product/smetana-1"):
    return {
        "id": str(uuid.uuid4()), "normalized_name": name,
        "quantity": Decimal("1"), "unit_code": "piece",
        "buy_quantity": Decimal(str(buy)), "covered_from_inventory": home,
        "matched_product_name": product, "matched_product_url": url,
        "pack_count": packs, "estimated_cost_kop": cost,
        "purchased_at": "2026-08-28T10:00:00" if purchased else None,
        "to_taste": to_taste, "category_slug": slug,
    }


def make_plan(items=None):
    return {"id": str(PLAN_ID), "starts_on": "2026-09-01", "days": 3, "shopping": items or []}


def default_items():
    return [
        item("сметана", purchased=True),
        item("молоко", cost=8900),
        item("яйцо", cost=12000),
        item("курица", slug=MEAT, cost=45000),
        item("соль", slug=None, buy=0, cost=None, packs=None, to_taste=True, product=None, url=None),
        item("картофель", slug=None, buy=0, cost=None, packs=None, product=None, url=None,
             home=Decimal("500")),
        item("готовая грудка в меду", slug=None, cost=None, packs=None, product=None, url=None),
    ]


class _AppRepo:
    def __init__(self, plan=None):
        self.plan = plan

    async def get_plan(self, session, plan_id):
        return self.plan

    async def latest_plan(self, session):
        return self.plan


class _BotRepo:
    async def context_for_user(self, user_id):
        return CONTEXT

    async def latest_plan_meals(self, household_id):
        return []

    async def shopping_items(self, household_id):
        return []


def buttons(reply):
    return [b for row in reply.keyboard["inline_keyboard"] for b in row]


class CategoryLabelTests(unittest.TestCase):
    """Названия разделов лежат и в питоне, и в JS — они не должны разъехаться."""

    def test_python_and_js_maps_match(self) -> None:
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "web", "static", "js", "format.js"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        block = re.search(r"CATEGORY_LABELS = \{(.*?)\n\};", source, re.S).group(1)
        js_map = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block))
        self.assertEqual(js_map, CATEGORY_LABELS)

    def test_unknown_slug_becomes_words(self) -> None:
        self.assertEqual(category_label("novyj-razdel-777"), "Novyj razdel")

    def test_no_slug_means_unmatched(self) -> None:
        self.assertEqual(category_label(None), "Уточнить в магазине")


class SplitTests(unittest.TestCase):
    def test_items_are_split_into_three_kinds(self) -> None:
        groups, taste, covered = shopping.split_items(default_items())
        self.assertEqual(sorted(groups), sorted([DAIRY, MEAT, shopping.NO_CATEGORY]))
        self.assertEqual([entry["normalized_name"] for entry in taste], ["соль"])
        self.assertEqual([entry["normalized_name"] for entry in covered], ["картофель"])

    def test_unmatched_section_goes_last(self) -> None:
        groups, _taste, _covered = shopping.split_items(default_items())
        order = shopping._sorted_slugs(groups)
        self.assertEqual(order[-1], shopping.NO_CATEGORY)


class OverviewTests(unittest.TestCase):
    def test_sections_show_progress(self) -> None:
        reply = shopping.overview_reply(make_plan(default_items()))
        labels = [button["text"] for button in buttons(reply)]
        self.assertIn("Молочные продукты, яйцо · 1/3", labels)
        self.assertIn("Мясо и птица · 0/1", labels)

    def test_header_counts_money_and_items(self) -> None:
        text = shopping.overview_reply(make_plan(default_items())).text
        self.assertIn("куплено 1 из 5", text)
        self.assertIn("осталось", text.lower())

    def test_taste_group_is_separate(self) -> None:
        labels = [b["text"] for b in buttons(shopping.overview_reply(make_plan(default_items())))]
        self.assertTrue(any("По вкусу и уточнить · 1" in label for label in labels))

    def test_everything_bought_is_celebrated(self) -> None:
        items = [item("молоко", purchased=True), item("яйцо", purchased=True)]
        self.assertIn("Всё куплено", shopping.overview_reply(make_plan(items)).text)

    def test_empty_list_says_so(self) -> None:
        reply = shopping.overview_reply(make_plan([]))
        self.assertIn("Списка покупок нет", reply.text)
        self.assertIsNone(reply.keyboard)

    def test_overview_offers_flat_list_and_menu(self) -> None:
        labels = [b["text"] for b in buttons(shopping.overview_reply(make_plan(default_items())))]
        self.assertIn("📋 Все подряд", labels)
        self.assertIn("📅 Меню", labels)


class CategoryTests(unittest.TestCase):
    def test_checklist_marks_and_prices(self) -> None:
        reply = shopping.category_reply(make_plan(default_items()), DAIRY)
        labels = [b["text"] for b in buttons(reply) if b["text"].startswith(("☐", "✅"))]
        self.assertTrue(any(label.startswith("✅ сметана") for label in labels))
        self.assertTrue(any("2 уп" in label and "89 ₽" in label for label in labels))

    def test_toggle_button_returns_to_the_section(self) -> None:
        reply = shopping.category_reply(make_plan(default_items()), DAIRY)
        data = next(b["callback_data"] for b in buttons(reply) if b["text"].startswith("☐"))
        verb, parts = parse_callback(data)
        self.assertEqual((verb, parts[-1]), ("s", "c"))

    def test_taste_section_has_no_checkboxes(self) -> None:
        reply = shopping.category_reply(make_plan(default_items()), shopping.TASTE)
        self.assertIn("соль", reply.text)
        verbs = [parse_callback(b["callback_data"])[0] for b in buttons(reply)]
        self.assertNotIn("s", verbs)

    def test_finished_section_says_so(self) -> None:
        items = [item("молоко", purchased=True)]
        self.assertIn("Всё куплено в этом разделе",
                      shopping.category_reply(make_plan(items), DAIRY).text)

    def test_vanished_section_does_not_crash(self) -> None:
        reply = shopping.category_reply(make_plan(default_items()), "kofe-chajj-kakao-242")
        self.assertIn("опустел", reply.text)

    def test_long_section_is_paginated_by_thirty(self) -> None:
        items = [item(f"позиция {index}") for index in range(70)]
        reply = shopping.category_reply(make_plan(items), DAIRY, 2)
        checkboxes = [b for b in buttons(reply) if b["text"].startswith("☐")]
        self.assertEqual(len(checkboxes), 30)
        self.assertIn("Страница 2 из 3", reply.text)
        pager = [b for b in buttons(reply) if parse_callback(b["callback_data"])[0] == "p"]
        self.assertEqual([b["text"] for b in pager], ["◀", "▶"])

    def test_section_keyboard_never_exceeds_the_limit(self) -> None:
        """§9.8: сто кнопок — жёсткий лимит Bot API."""
        items = [item(f"позиция {index}") for index in range(120)]
        for page in (1, 2, 3, 4):
            reply = shopping.category_reply(make_plan(items), DAIRY, page)
            self.assertLessEqual(len(buttons(reply)), 100)


class DetailsTests(unittest.TestCase):
    def test_details_show_home_stock_packs_and_link(self) -> None:
        text = shopping.details_reply(make_plan(default_items()), DAIRY).text
        self.assertIn("Сметана 20 %", text)
        self.assertIn("2 уп", text)
        self.assertIn("https://lenta.com/product/smetana-1", text)

    def test_details_list_what_is_already_at_home(self) -> None:
        text = shopping.details_reply(make_plan(default_items()), shopping.NO_CATEGORY).text
        self.assertIn("хватает домашних запасов", text)
        self.assertIn("картофель", text)

    def test_unmatched_item_says_to_ask_in_store(self) -> None:
        text = shopping.details_reply(make_plan(default_items()), shopping.NO_CATEGORY).text
        self.assertIn("уточните в магазине", text)


class FlatListTests(unittest.TestCase):
    def test_flat_list_covers_every_item(self) -> None:
        items = [item(f"позиция {index}") for index in range(70)]
        seen = set()
        for page in (1, 2, 3):
            reply = shopping.flat_reply(make_plan(items), page)
            seen.update(b["text"] for b in buttons(reply) if b["text"].startswith("☐"))
        self.assertEqual(len(seen), 70)

    def test_flat_toggle_returns_to_the_same_page(self) -> None:
        items = [item(f"позиция {index}") for index in range(70)]
        reply = shopping.flat_reply(make_plan(items), 2)
        data = next(b["callback_data"] for b in buttons(reply) if b["text"].startswith("☐"))
        self.assertEqual(parse_callback(data)[1][-1], "2")

    def test_nothing_to_buy(self) -> None:
        items = [item("соль", buy=0, to_taste=True)]
        self.assertIn("Покупать нечего", shopping.flat_reply(make_plan(items)).text)


class DispatchTests(unittest.TestCase):
    def test_shopping_command_opens_sections(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "🛒 Покупки", TODAY,
            app_repository=_AppRepo(make_plan(default_items())), dialogs=object(),
        ))
        self.assertIn("Выберите раздел", reply.text)

    def test_shopping_without_plan(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "/shopping", TODAY,
            app_repository=_AppRepo(None), dialogs=object(),
        ))
        self.assertIn("сначала составьте меню", reply.text)

    def test_section_button_opens_checklist(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(make_plan(default_items())), _BotRepo(), USER_ID,
            encode_callback("f", "sh", pack_uuid(PLAN_ID), MEAT), TODAY,
        ))
        self.assertIn("Мясо и птица", result.edit.text)

    def test_back_button_returns_to_sections(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(make_plan(default_items())), _BotRepo(), USER_ID,
            encode_callback("f", "sh", pack_uuid(PLAN_ID), ""), TODAY,
        ))
        self.assertIn("Выберите раздел", result.edit.text)

    def test_details_button(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(make_plan(default_items())), _BotRepo(), USER_ID,
            encode_callback("f", "sh", pack_uuid(PLAN_ID), DAIRY, "i"), TODAY,
        ))
        self.assertIn("подробно", result.edit.text)

    def test_section_page_button(self) -> None:
        items = [item(f"позиция {index}") for index in range(70)]
        result = run_async(handle_callback(
            _AppRepo(make_plan(items)), _BotRepo(), USER_ID,
            encode_callback("p", "sc", pack_uuid(PLAN_ID), DAIRY, 3), TODAY,
        ))
        self.assertIn("Страница 3 из 3", result.edit.text)

    def test_flat_page_button(self) -> None:
        items = [item(f"позиция {index}") for index in range(70)]
        result = run_async(handle_callback(
            _AppRepo(make_plan(items)), _BotRepo(), USER_ID,
            encode_callback("p", "sh", pack_uuid(PLAN_ID), 2), TODAY,
        ))
        self.assertIn("Страница 2 из 3", result.edit.text)

    def test_stale_plan_button(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(None), _BotRepo(), USER_ID,
            encode_callback("f", "sh", pack_uuid(PLAN_ID), DAIRY), TODAY,
        ))
        self.assertTrue(result.show_alert)


class CallbackSizeTests(unittest.TestCase):
    def test_every_shopping_button_fits_in_64_bytes(self) -> None:
        """Самый длинный слаг каталога — 27 символов; бюджет 64 байта тесный."""
        longest = max(CATEGORY_LABELS, key=len)
        items = [item(f"позиция {index}", slug=longest) for index in range(70)]
        plan = make_plan(items)
        screens = [
            shopping.overview_reply(plan),
            shopping.category_reply(plan, longest, 2),
            shopping.details_reply(plan, longest),
            shopping.flat_reply(plan, 2),
        ]
        for reply in screens:
            for button in buttons(reply):
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
