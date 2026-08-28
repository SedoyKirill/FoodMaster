"""Библиотека рецептов в боте: поиск, фильтры, карточка, оценка (T7, §5.7)."""

import asyncio
import os
import re
import sys
import unittest
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.telegram.callbacks import encode_callback, pack_uuid, parse_callback
from app.telegram.fsm import DialogState
from app.telegram.scenes import SceneContext, recipes
from app.telegram.dispatch import handle_callback, handle_message
from app.web.categories import CUISINES, DISH_TYPES

USER_ID = 7
TODAY = date(2026, 8, 28)
PLAN_ID = uuid.uuid4()
CONTEXT = {
    "user_id": str(uuid.uuid4()), "login": "tg7", "role": "owner",
    "household_id": str(uuid.uuid4()), "household_name": "Моя семья",
}
VIEWER = {**CONTEXT, "role": "viewer"}


def run_async(coro):
    return asyncio.run(coro)


def recipe(index=1, *, title=None, status="ready", minutes=35, servings=4):
    return {
        "id": index, "title": title or f"Плов узбекский {index}",
        "review_status": status, "time_total_minutes": minutes,
        "source_servings_min": servings, "cuisine_code": "asian",
        "dish_type": "main_course", "source_page_start": 10 + index,
        "ingredient_names": ["рис", "баранина"],
    }


def detail(index=1, *, rating=None, status="ready", price=12300):
    return {
        "id": index, "title": f"Плов узбекский {index}", "review_status": status,
        "source_page_start": 11, "source_servings_min": 4, "time_total_minutes": 35,
        "my_rating": rating,
        "ingredients": [
            {
                "raw_text": "Рис — 300 г", "normalized_name": "рис",
                "matched_product": {
                    "name": "Рис «Мистраль»", "effective_price_kop": price,
                } if price else None,
            },
            {"raw_text": "Соль", "normalized_name": "соль", "is_to_taste": True},
        ],
        "steps": [{"position": 1, "instruction": "Обжарить."}],
    }


def plan_with_days(days=3):
    meals = []
    for day in range(days):
        for meal_type in ("breakfast", "lunch", "dinner"):
            meals.append({
                "id": str(uuid.uuid4()), "meal_date": date(2026, 9, 1 + day).isoformat(),
                "meal_type": meal_type, "recipe_id": 1, "title": "Блюдо",
                "estimated_kcal": 500, "warnings": [],
            })
    return {"id": str(PLAN_ID), "starts_on": "2026-09-01", "days": days,
            "estimated_cost_kop": 100000, "meals": meals, "shopping": [], "warnings": []}


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
    def __init__(self, *, items=None, total=None, card=None, plan=None,
                 facets=None, rating_error=None, review_error=None, add_error=None):
        self.items = items if items is not None else [recipe(index) for index in range(1, 9)]
        self.total = total if total is not None else len(self.items)
        self.card = card or detail()
        self.plan = plan
        self.facets = facets or {"cuisines": ["asian", "georgian"],
                                 "dish_types": ["soup", "main_course"]}
        self.rating_error = rating_error
        self.review_error = review_error
        self.add_error = add_error
        self.list_calls = []
        self.rating_calls = []
        self.review_calls = []
        self.add_calls = []

    async def list_recipes(self, **kwargs):
        self.list_calls.append(kwargs)
        return {"items": self.items, "total": self.total,
                "limit": kwargs.get("limit"), "offset": kwargs.get("offset")}

    async def recipe_facets(self):
        return self.facets

    async def recipe_detail(self, recipe_id, household_id=None):
        return self.card if self.card and int(self.card["id"]) == int(recipe_id) else None

    async def set_recipe_rating(self, session, recipe_id, rating):
        if self.rating_error:
            raise self.rating_error
        self.rating_calls.append((recipe_id, rating))
        self.card = {**self.card, "my_rating": rating}
        return self.card

    async def set_review_status(self, session, recipe_id, status):
        if self.review_error:
            raise self.review_error
        self.review_calls.append((recipe_id, status))
        self.card = {**self.card, "review_status": status}
        return self.card

    async def latest_plan(self, session):
        return self.plan

    async def add_to_plan(self, session, plan_id, meal_date, meal_type, recipe_id):
        if self.add_error:
            raise self.add_error
        self.add_calls.append((plan_id, meal_date, meal_type, recipe_id))
        return self.plan


class _BotRepo:
    def __init__(self, context=CONTEXT):
        self.context = context

    async def context_for_user(self, user_id):
        return self.context

    async def latest_plan_meals(self, household_id):
        return []

    async def shopping_items(self, household_id):
        return []


def buttons(reply):
    return [b for row in reply.keyboard["inline_keyboard"] for b in row]


class LabelDriftTests(unittest.TestCase):
    """Кухни и типы блюд продублированы в JS — расхождение должно падать."""

    def _js_map(self, name):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "web", "static", "js", "format.js"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        block = re.search(rf"{name} = \{{(.*?)\n\}};", source, re.S).group(1)
        return dict(re.findall(r'(\w+):\s*"([^"]+)"', block))

    def test_cuisines_match_js(self) -> None:
        self.assertEqual(self._js_map("CUISINES"), CUISINES)

    def test_dish_types_match_js(self) -> None:
        self.assertEqual(self._js_map("DISH_TYPES"), DISH_TYPES)


class SearchTests(unittest.TestCase):
    def test_begin_opens_scene_and_lists(self) -> None:
        dialogs = _Dialogs()
        reply = run_async(recipes.begin(dialogs, _AppRepo(), USER_ID))
        self.assertEqual(dialogs.state.scene, recipes.SCENE)
        self.assertIn("Нашёл 8", reply.text)

    def test_free_text_is_the_query(self) -> None:
        dialogs = _Dialogs(DialogState(recipes.SCENE, "query", {}))
        app_repository = _AppRepo()
        ctx = SceneContext(
            actor=type("A", (), {"user_id": USER_ID, "chat_id": USER_ID})(),
            text="  плов  ", state=dialogs.state, bot_repository=_BotRepo(),
            app_repository=app_repository, dialogs=dialogs, today=TODAY,
        )
        reply = run_async(recipes.handle_step(ctx))
        self.assertEqual(app_repository.list_calls[0]["search"], "плов")
        self.assertIn("«плов»", reply.text)
        self.assertEqual(dialogs.state.data["q"], "плов")

    def test_cards_show_time_servings_and_draft(self) -> None:
        app_repository = _AppRepo(items=[recipe(1, status="needs_review")])
        reply = run_async(recipes.results_reply(app_repository, {}))
        label = buttons(reply)[0]["text"]
        self.assertIn("35 мин", label)
        self.assertIn("4 порц.", label)
        self.assertIn("черновик", label)

    def test_pagination_by_eight(self) -> None:
        app_repository = _AppRepo(total=30)
        reply = run_async(recipes.results_reply(app_repository, {"page": 2}))
        self.assertIn("страница 2 из 4", reply.text)
        self.assertEqual(app_repository.list_calls[0]["offset"], 8)
        pager = [b["text"] for b in buttons(reply)
                 if parse_callback(b["callback_data"])[0] == "p"]
        self.assertEqual(pager, ["◀", "▶"])

    def test_empty_result_explains_itself(self) -> None:
        reply = run_async(recipes.results_reply(
            _AppRepo(items=[], total=0), {"q": "динозавр", "ready": True}
        ))
        self.assertIn("Ничего не нашлось", reply.text)
        self.assertIn("только проверенные", reply.text)
        self.assertIn("Снимите фильтр", reply.text)


class FilterTests(unittest.TestCase):
    def test_ready_toggle(self) -> None:
        dialogs = _Dialogs(DialogState(recipes.SCENE, "query", {}))
        run_async(recipes.handle_filter(_AppRepo(), dialogs, USER_ID, ["rc", "r"]))
        self.assertTrue(dialogs.state.data["ready"])
        run_async(recipes.handle_filter(_AppRepo(), dialogs, USER_ID, ["rc", "r"]))
        self.assertFalse(dialogs.state.data["ready"])

    def test_cuisine_choices_come_from_library(self) -> None:
        result = run_async(recipes.handle_filter(
            _AppRepo(), _Dialogs(DialogState(recipes.SCENE, "query", {})), USER_ID,
            ["rc", "c"],
        ))
        labels = [b["text"] for b in buttons(result.edit)]
        self.assertIn("☐ Азиатская", labels)
        self.assertIn("☐ Грузинская", labels)
        self.assertIn("Не важно", labels)

    def test_applying_cuisine_filters_the_query(self) -> None:
        dialogs = _Dialogs(DialogState(recipes.SCENE, "query", {}))
        app_repository = _AppRepo()
        run_async(recipes.handle_filter(
            app_repository, dialogs, USER_ID, ["rc", "c", "georgian"]
        ))
        self.assertEqual(dialogs.state.data["cuisine"], "georgian")
        self.assertEqual(app_repository.list_calls[0]["cuisine"], "georgian")

    def test_not_important_clears_one_filter(self) -> None:
        dialogs = _Dialogs(DialogState(recipes.SCENE, "query", {"cuisine": "asian"}))
        run_async(recipes.handle_filter(_AppRepo(), dialogs, USER_ID, ["rc", "c", "-"]))
        self.assertEqual(dialogs.state.data["cuisine"], "")

    def test_reset_clears_everything(self) -> None:
        dialogs = _Dialogs(DialogState(
            recipes.SCENE, "query", {"q": "плов", "cuisine": "asian", "ready": True}
        ))
        run_async(recipes.handle_filter(_AppRepo(), dialogs, USER_ID, ["rc", "x"]))
        self.assertEqual(dialogs.state.data, {})

    def test_meal_choices_are_static(self) -> None:
        result = run_async(recipes.handle_filter(
            _AppRepo(), _Dialogs(DialogState(recipes.SCENE, "query", {})), USER_ID,
            ["rc", "m"],
        ))
        labels = [b["text"] for b in buttons(result.edit)]
        self.assertIn("☐ Завтрак", labels)

    def test_page_button_keeps_filters(self) -> None:
        dialogs = _Dialogs(DialogState(recipes.SCENE, "query", {"q": "плов"}))
        app_repository = _AppRepo(total=30)
        run_async(recipes.handle_page(app_repository, dialogs, USER_ID, ["rc", "3"]))
        self.assertEqual(app_repository.list_calls[0]["search"], "плов")
        self.assertEqual(app_repository.list_calls[0]["offset"], 16)


class CardTests(unittest.TestCase):
    def test_card_shows_recipe_and_stars(self) -> None:
        reply = recipes.card_reply(detail(rating=3), CONTEXT)
        self.assertIn("Плов узбекский", reply.text)
        stars = [b["text"] for b in buttons(reply)
                 if parse_callback(b["callback_data"])[0] == "g"]
        self.assertEqual(stars[:5], ["★", "★", "★", "☆", "☆"])
        self.assertIn("Снять оценку", [b["text"] for b in buttons(reply)])

    def test_unrated_card_has_no_remove_button(self) -> None:
        labels = [b["text"] for b in buttons(recipes.card_reply(detail(), CONTEXT))]
        self.assertNotIn("Снять оценку", labels)

    def test_review_buttons_only_for_owner(self) -> None:
        owner = [parse_callback(b["callback_data"])[0]
                 for b in buttons(recipes.card_reply(detail(), CONTEXT))]
        viewer = [parse_callback(b["callback_data"])[0]
                  for b in buttons(recipes.card_reply(detail(), VIEWER))]
        self.assertIn("w", owner)
        self.assertNotIn("w", viewer)

    def test_current_status_is_not_offered_again(self) -> None:
        reply = recipes.card_reply(detail(status="ready"), CONTEXT)
        statuses = [parse_callback(b["callback_data"])[1][1] for b in buttons(reply)
                    if parse_callback(b["callback_data"])[0] == "w"]
        self.assertNotIn("ready", statuses)
        self.assertIn("rejected", statuses)

    def test_prices_toggle(self) -> None:
        without = recipes.card_reply(detail(), CONTEXT)
        self.assertNotIn("Цены «Ленты»", without.text)
        with_prices = recipes.card_reply(detail(), CONTEXT, with_prices=True)
        self.assertIn("Рис «Мистраль» — 123 ₽", with_prices.text)
        self.assertIn("📖 Без цен", [b["text"] for b in buttons(with_prices)])

    def test_prices_without_matches_say_so(self) -> None:
        reply = recipes.card_reply(detail(price=None), CONTEXT, with_prices=True)
        self.assertIn("Ни один ингредиент не сопоставлен", reply.text)

    def test_rating_updates_the_card(self) -> None:
        app_repository = _AppRepo()
        result = run_async(recipes.rate(app_repository, CONTEXT, 1, 4))
        self.assertEqual(app_repository.rating_calls, [(1, 4)])
        self.assertIn("Оценка: 4", result.toast)

    def test_zero_removes_the_rating(self) -> None:
        app_repository = _AppRepo(card=detail(rating=4))
        result = run_async(recipes.rate(app_repository, CONTEXT, 1, 0))
        self.assertEqual(app_repository.rating_calls, [(1, None)])
        self.assertIn("снята", result.toast)

    def test_viewer_cannot_rate(self) -> None:
        app_repository = _AppRepo(rating_error=PermissionError("Режим просмотра"))
        result = run_async(recipes.rate(app_repository, VIEWER, 1, 5))
        self.assertTrue(result.show_alert)
        self.assertIn("Режим просмотра", result.toast)

    def test_review_status_changes(self) -> None:
        app_repository = _AppRepo(card=detail(status="needs_review"))
        result = run_async(recipes.review(app_repository, CONTEXT, 1, "ready"))
        self.assertEqual(app_repository.review_calls, [(1, "ready")])
        self.assertIn("Готов", result.toast)

    def test_editor_cannot_review(self) -> None:
        app_repository = _AppRepo(review_error=PermissionError("Недостаточно прав"))
        result = run_async(recipes.review(app_repository, CONTEXT, 1, "ready"))
        self.assertIn("Недостаточно прав", result.toast)


class AddToPlanTests(unittest.TestCase):
    def test_no_plan_says_so(self) -> None:
        result = run_async(recipes.choose_day(_AppRepo(plan=None), CONTEXT, 1))
        self.assertTrue(result.show_alert)
        self.assertIn("составьте меню", result.toast)

    def test_day_picker_lists_plan_days(self) -> None:
        result = run_async(recipes.choose_day(_AppRepo(plan=plan_with_days(3)), CONTEXT, 1))
        days = [b["text"] for b in buttons(result.edit) if b["text"].startswith("День")]
        self.assertEqual(len(days), 3)
        self.assertIn("01.09", days[0])

    def test_meal_picker_offers_three_slots(self) -> None:
        result = run_async(recipes.choose_meal(
            _AppRepo(plan=plan_with_days(3)), CONTEXT, 1, 2
        ))
        labels = [b["text"] for b in buttons(result.edit)]
        self.assertEqual(labels[:3], ["Завтрак", "Обед", "Ужин"])

    def test_put_in_plan_calls_repository(self) -> None:
        app_repository = _AppRepo(plan=plan_with_days(3))
        result = run_async(recipes.put_in_plan(app_repository, CONTEXT, 7, 2, "dinner"))
        plan_id, meal_date, meal_type, recipe_id = app_repository.add_calls[0]
        self.assertEqual((meal_date, meal_type, recipe_id), (date(2026, 9, 2), "dinner", 7))
        self.assertIn("День 2 из 3", result.edit.text)

    def test_unsuitable_recipe_explains_why(self) -> None:
        app_repository = _AppRepo(
            plan=plan_with_days(3),
            add_error=ValueError("Этот рецепт нельзя поставить в выбранный слот"),
        )
        result = run_async(recipes.put_in_plan(app_repository, CONTEXT, 7, 1, "breakfast"))
        self.assertIn("нельзя поставить", result.edit.text)
        self.assertIn("технике или ограничениям", result.edit.text)

    def test_cuisine_filter_is_named_as_the_reason(self) -> None:
        """Кухня — жёсткий фильтр плана, но пользователю про это нигде не сказано."""
        plan = {**plan_with_days(3), "cuisine_preferences": ["asian"]}
        app_repository = _AppRepo(
            plan=plan, card={**detail(1), "cuisine_code": "middle_eastern"},
            add_error=ValueError("Этот рецепт нельзя поставить в выбранный слот"),
        )
        result = run_async(recipes.put_in_plan(app_repository, CONTEXT, 1, 1, "lunch"))
        self.assertIn("Азиатская", result.edit.text)
        self.assertIn("ближневосточная", result.edit.text)

    def test_day_out_of_range(self) -> None:
        result = run_async(recipes.put_in_plan(
            _AppRepo(plan=plan_with_days(3)), CONTEXT, 7, 9, "dinner"
        ))
        self.assertTrue(result.show_alert)


class DispatchTests(unittest.TestCase):
    def test_recipes_button_opens_search(self) -> None:
        dialogs = _Dialogs()
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "📖 Рецепты", TODAY,
            app_repository=_AppRepo(), dialogs=dialogs,
        ))
        self.assertEqual(dialogs.state.scene, recipes.SCENE)
        self.assertIn("Нашёл", reply.text)

    def test_library_card_is_addressed_by_number(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(), _BotRepo(), USER_ID, encode_callback("r", 1), TODAY,
            dialogs=_Dialogs(),
        ))
        self.assertIn("Плов узбекский", result.edit.text)

    def test_plan_card_still_addressed_by_uuids(self) -> None:
        """Старая кнопка из плана — пара UUID, её нельзя спутать с номером."""
        data = encode_callback("r", pack_uuid(PLAN_ID), pack_uuid(uuid.uuid4()))
        verb, parts = parse_callback(data)
        self.assertFalse(parts[0].isdigit())

    def test_prices_button(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(), _BotRepo(), USER_ID, encode_callback("r", 1, "p"), TODAY,
            dialogs=_Dialogs(),
        ))
        self.assertIn("Цены «Ленты»", result.edit.text)

    def test_rating_button(self) -> None:
        app_repository = _AppRepo()
        run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID, encode_callback("g", 1, 5), TODAY,
            dialogs=_Dialogs(),
        ))
        self.assertEqual(app_repository.rating_calls, [(1, 5)])

    def test_review_button(self) -> None:
        app_repository = _AppRepo(card=detail(status="needs_review"))
        run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID, encode_callback("w", 1, "ready"), TODAY,
            dialogs=_Dialogs(),
        ))
        self.assertEqual(app_repository.review_calls, [(1, "ready")])

    def test_back_to_search(self) -> None:
        dialogs = _Dialogs(DialogState(recipes.SCENE, "query", {"q": "плов"}))
        result = run_async(handle_callback(
            _AppRepo(), _BotRepo(), USER_ID, encode_callback("n", "rc", "back"), TODAY,
            dialogs=dialogs,
        ))
        self.assertIn("«плов»", result.edit.text)

    def test_add_to_plan_chain(self) -> None:
        app_repository = _AppRepo(plan=plan_with_days(3))
        day = run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID,
            encode_callback("n", "rc", "add", 7), TODAY, dialogs=_Dialogs(),
        ))
        self.assertIn("В какой день", day.edit.text)
        meal = run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID,
            encode_callback("n", "rc", "day", 7, 2), TODAY, dialogs=_Dialogs(),
        ))
        self.assertIn("приём пищи", meal.edit.text)
        run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID,
            encode_callback("n", "rc", "set", 7, 2, "lunch"), TODAY, dialogs=_Dialogs(),
        ))
        self.assertEqual(app_repository.add_calls[0][2:], ("lunch", 7))

    def test_every_button_fits_in_64_bytes(self) -> None:
        screens = [
            run_async(recipes.results_reply(_AppRepo(total=30), {"page": 2})),
            recipes.card_reply(detail(rating=3), CONTEXT),
            run_async(recipes.choose_day(_AppRepo(plan=plan_with_days(7)), CONTEXT, 99999)).edit,
            run_async(recipes.choose_meal(
                _AppRepo(plan=plan_with_days(7)), CONTEXT, 99999, 7
            )).edit,
        ]
        for reply in screens:
            for button in buttons(reply):
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
