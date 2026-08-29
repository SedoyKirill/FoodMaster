"""Меню в боте: мастер, просмотр по дням, история, удаление (TZ-M7 T5, §5.3–5.5)."""

import asyncio
import os
import sys
import unittest
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.telegram.callbacks import encode_callback, heavy_placeholder, parse_callback
from app.telegram.fsm import DialogState
from app.telegram.scenes import SceneContext, plan
from app.telegram.dispatch import handle_callback, handle_message

USER_ID = 7
TODAY = date(2026, 8, 28)          # пятница
PLAN_ID = uuid.uuid4()
MEAL_IDS = [uuid.uuid4() for _ in range(3)]
CONTEXT = {
    "user_id": str(uuid.uuid4()), "login": "tg7", "role": "owner",
    "household_id": str(uuid.uuid4()), "household_name": "Моя семья",
}


def run_async(coro):
    return asyncio.run(coro)


def make_plan(days=3, budget_kop=500000, cost_kop=412000, plan_id=PLAN_ID):
    meals = []
    for day in range(days):
        for index, meal_type in enumerate(("breakfast", "lunch", "dinner")):
            meals.append({
                "id": str(MEAL_IDS[index]) if day == 0 else str(uuid.uuid4()),
                "meal_date": date(2026, 9, 1 + day).isoformat(),
                "meal_type": meal_type,
                "recipe_id": 10 + index,
                "title": f"Блюдо {day}-{index}",
                "estimated_kcal": 500 + index,
                "warnings": ["scale_unknown"] if (day, index) == (0, 0) else [],
            })
    return {
        "id": str(plan_id), "starts_on": "2026-09-01", "days": days,
        "budget_kop": budget_kop, "estimated_cost_kop": cost_kop,
        "matched_cost_items": 23, "total_cost_items": 25,
        "mode": "balanced", "warnings": ["Рецепты требуют проверки."],
        "meals": meals, "shopping": [],
    }


class _Dialogs:
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


class _AppRepo:
    def __init__(self, *, plan=None, plans=None, cuisines=("georgian", "asian"),
                 create_error=None, deleted=True):
        self.plan = plan
        self.plans = plans if plans is not None else []
        self.cuisines = list(cuisines)
        self.create_error = create_error
        self.deleted = deleted
        self.create_calls = []
        self.delete_calls = []

    async def recipe_facets(self):
        return {"cuisines": self.cuisines}

    async def create_plan(self, session, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return self.plan or make_plan()

    async def get_plan(self, session, plan_id):
        return self.plan

    async def latest_plan(self, session):
        return self.plan

    async def list_plans(self, session, limit=20):
        return self.plans

    async def delete_plan(self, session, plan_id):
        self.delete_calls.append(plan_id)
        return self.deleted


class _BotRepo:
    def __init__(self, context=CONTEXT):
        self.context = context

    async def context_for_user(self, user_id):
        return self.context

    async def latest_plan_meals(self, household_id):
        return []

    async def shopping_items(self, household_id):
        return []


def _ctx(text, state, app_repository, dialogs):
    return SceneContext(
        actor=type("A", (), {"user_id": USER_ID, "chat_id": USER_ID})(),
        text=text, state=state, bot_repository=_BotRepo(),
        app_repository=app_repository, dialogs=dialogs, today=TODAY,
    )


class ParseTests(unittest.TestCase):
    def test_relative_days(self) -> None:
        self.assertEqual(plan.parse_start_date("сегодня", TODAY), TODAY)
        self.assertEqual(plan.parse_start_date("Завтра", TODAY), date(2026, 8, 29))
        self.assertEqual(plan.parse_start_date("послезавтра", TODAY), date(2026, 8, 30))

    def test_weekday_means_the_next_one(self) -> None:
        # пятница 28 августа → «понедельник» это 31-е, а не сегодня
        self.assertEqual(plan.parse_start_date("понедельник", TODAY), date(2026, 8, 31))
        self.assertEqual(plan.parse_start_date("пятница", TODAY), date(2026, 9, 4))

    def test_numeric_dates(self) -> None:
        self.assertEqual(plan.parse_start_date("12.09", TODAY), date(2026, 9, 12))
        self.assertEqual(plan.parse_start_date("12.09.2027", TODAY), date(2027, 9, 12))
        self.assertIsNone(plan.parse_start_date("32.13", TODAY))

    def test_worded_date_rolls_over_the_year(self) -> None:
        self.assertEqual(plan.parse_start_date("12 сентября", TODAY), date(2026, 9, 12))
        # январь уже прошёл в этом году — значит речь про следующий
        self.assertEqual(plan.parse_start_date("5 января", TODAY), date(2027, 1, 5))

    def test_garbage_date(self) -> None:
        self.assertIsNone(plan.parse_start_date("когда-нибудь", TODAY))
        self.assertIsNone(plan.parse_start_date("", TODAY))

    def test_days(self) -> None:
        self.assertEqual(plan.parse_days("5"), 5)
        self.assertEqual(plan.parse_days("на 7 дней"), 7)
        self.assertIsNone(plan.parse_days("100"))
        self.assertIsNone(plan.parse_days("много"))

    def test_budget(self) -> None:
        self.assertEqual(plan.parse_budget("5000"), 500000)
        self.assertEqual(plan.parse_budget("4 500 ₽"), 450000)
        self.assertIsNone(plan.parse_budget("без бюджета"))
        self.assertIsInstance(plan.parse_budget("сколько-то"), str)
        self.assertIsInstance(plan.parse_budget("-5"), str)


class WizardTests(unittest.TestCase):
    def test_begin_opens_first_step(self) -> None:
        dialogs = _Dialogs()
        reply = run_async(plan.begin(dialogs, USER_ID))
        self.assertEqual((dialogs.state.scene, dialogs.state.step), (plan.SCENE, "start"))
        self.assertIn("С какого дня", reply.text)

    def test_typed_date_moves_to_days(self) -> None:
        dialogs = _Dialogs(DialogState(plan.SCENE, "start", {}))
        reply = run_async(plan.handle_step(_ctx("12.09", dialogs.state, _AppRepo(), dialogs)))
        self.assertEqual(dialogs.state.step, "days")
        self.assertEqual(dialogs.state.data["starts_on"], "2026-09-12")
        # §4.2: на каждом шаге видно, что уже выбрано
        self.assertIn("Старт:", reply.text)

    def test_bad_date_repeats_the_question(self) -> None:
        dialogs = _Dialogs(DialogState(plan.SCENE, "start", {}))
        reply = run_async(plan.handle_step(_ctx("ага", dialogs.state, _AppRepo(), dialogs)))
        self.assertEqual(dialogs.state.step, "start")
        self.assertIn("Не понял дату", reply.text)

    def test_quick_button_sets_value(self) -> None:
        dialogs = _Dialogs(DialogState(plan.SCENE, "days", {"starts_on": "2026-09-01"}))
        run_async(plan.handle_callback(
            _AppRepo(), dialogs, CONTEXT, USER_ID, ["pl", "days", "5"], TODAY
        ))
        self.assertEqual(dialogs.state.data["days"], 5)
        self.assertEqual(dialogs.state.step, "budget")

    def test_budget_none_is_kept(self) -> None:
        dialogs = _Dialogs(DialogState(plan.SCENE, "budget", {}))
        run_async(plan.handle_callback(
            _AppRepo(), dialogs, CONTEXT, USER_ID, ["pl", "budget", "none"], TODAY
        ))
        self.assertIsNone(dialogs.state.data["budget_kop"])
        self.assertEqual(dialogs.state.step, "mode")

    def test_cuisine_chips_toggle(self) -> None:
        dialogs = _Dialogs(DialogState(plan.SCENE, "cuisines", {}))
        app_repository = _AppRepo()
        run_async(plan.toggle_cuisine(dialogs, app_repository, USER_ID, "georgian"))
        self.assertEqual(dialogs.state.data["cuisines"], ["georgian"])
        run_async(plan.toggle_cuisine(dialogs, app_repository, USER_ID, "georgian"))
        self.assertEqual(dialogs.state.data["cuisines"], [])

    def test_any_cuisine_clears_selection(self) -> None:
        dialogs = _Dialogs(DialogState(plan.SCENE, "cuisines", {"cuisines": ["asian"]}))
        run_async(plan.handle_callback(
            _AppRepo(), dialogs, CONTEXT, USER_ID, ["pl", "cuisines", "any"], TODAY
        ))
        self.assertEqual(dialogs.state.data["cuisines"], [])
        self.assertEqual(dialogs.state.step, "confirm")

    def test_library_without_cuisines_skips_the_question(self) -> None:
        """Спрашивать про кухни, которых нет в библиотеке, незачем."""
        dialogs = _Dialogs(DialogState(plan.SCENE, "mode", {}))
        run_async(plan.handle_callback(
            _AppRepo(cuisines=[]), dialogs, CONTEXT, USER_ID, ["pl", "mode", "economy"], TODAY
        ))
        self.assertEqual(dialogs.state.step, "confirm")

    def test_mode_is_remembered(self) -> None:
        """Режим — это веса целевой функции, а не подпись в сводке (§6.4)."""
        dialogs = _Dialogs(DialogState(plan.SCENE, "mode", {}))
        run_async(plan.handle_callback(
            _AppRepo(), dialogs, CONTEXT, USER_ID, ["pl", "mode", "fitness"], TODAY
        ))
        self.assertEqual(dialogs.state.data["mode"], "fitness")

    def test_unknown_mode_is_rejected(self) -> None:
        dialogs = _Dialogs(DialogState(plan.SCENE, "mode", {}))
        result = run_async(plan.handle_callback(
            _AppRepo(), dialogs, CONTEXT, USER_ID, ["pl", "mode", "золотой"], TODAY
        ))
        self.assertEqual(result.toast, "Не понял кнопку.")

    def test_two_week_horizon_is_offered(self) -> None:
        """Две недели пришли с M8; до него мастер знал только неделю."""
        self.assertIn(14, plan.DAY_CHOICES)
        self.assertEqual(plan.parse_days("14"), 14)
        self.assertIsNone(plan.parse_days("15"))

    def test_stale_wizard_button_says_so(self) -> None:
        result = run_async(plan.handle_callback(
            _AppRepo(), _Dialogs(None), CONTEXT, USER_ID, ["pl", "days", "5"], TODAY
        ))
        self.assertTrue(result.show_alert)


class BuildTests(unittest.TestCase):
    def _dialogs(self):
        return _Dialogs(DialogState(plan.SCENE, "confirm", {
            "starts_on": "2026-09-01", "days": 5, "budget_kop": 500000,
            "mode": "economy", "cuisines": ["georgian"],
        }))

    def test_build_passes_collected_answers(self) -> None:
        dialogs = self._dialogs()
        app_repository = _AppRepo(plan=make_plan())
        run_async(plan.build(app_repository, dialogs, CONTEXT, USER_ID, TODAY))
        self.assertEqual(app_repository.create_calls[0], {
            "starts_on": date(2026, 9, 1), "days": 5, "budget_kop": 500000,
            "cuisines": ["georgian"], "mode": "economy",
        })
        self.assertEqual(dialogs.cleared, 1)  # мастер закрыт

    def test_build_shows_first_day(self) -> None:
        result = run_async(plan.build(
            _AppRepo(plan=make_plan()), self._dialogs(), CONTEXT, USER_ID, TODAY
        ))
        self.assertIn("День 1 из 3", result.edit.text)

    def test_no_recipes_returns_to_cuisines(self) -> None:
        """§5.3: «нет подходящих рецептов» — не тупик, а возврат к параметрам."""
        dialogs = self._dialogs()
        result = run_async(plan.build(
            _AppRepo(create_error=ValueError("Нет подходящих рецептов")),
            dialogs, CONTEXT, USER_ID, TODAY,
        ))
        self.assertIn("Нет подходящих рецептов", result.edit.text)
        self.assertEqual(dialogs.state.step, "cuisines")

    def test_viewer_cannot_build(self) -> None:
        dialogs = self._dialogs()
        result = run_async(plan.build(
            _AppRepo(create_error=PermissionError("Режим просмотра не позволяет")),
            dialogs, CONTEXT, USER_ID, TODAY,
        ))
        self.assertIn("Режим просмотра", result.edit.text)
        self.assertEqual(dialogs.cleared, 1)

    def test_build_button_is_heavy(self) -> None:
        """Сборка идёт в фоне с плейсхолдером, а не в конвейере апдейтов."""
        data = encode_callback("n", "pl", "go", 1)
        self.assertIn("Собираем меню", heavy_placeholder(data))
        self.assertIsNone(heavy_placeholder(encode_callback("n", "pl", "days", 5)))


class DayViewTests(unittest.TestCase):
    def test_header_shows_money_and_coverage(self) -> None:
        text = plan.plan_header(make_plan())
        self.assertIn("≈4120 ₽ из 5000 ₽", text)
        self.assertIn("23 из 25", text)
        self.assertIn("92 %", text)

    def test_header_flags_budget_overrun(self) -> None:
        text = plan.plan_header(make_plan(budget_kop=300000, cost_kop=412000))
        self.assertIn("бюджет превышен", text)

    def test_no_budget_no_overrun_line(self) -> None:
        text = plan.plan_header(make_plan(budget_kop=None))
        self.assertNotIn("бюджет превышен", text)

    def test_day_lists_meals_and_badges(self) -> None:
        reply = plan.day_reply(make_plan(), 1)
        self.assertIn("День 1 из 3", reply.text)
        self.assertIn("Завтрак: Блюдо 0-0", reply.text)
        self.assertIn("порции как в книге", reply.text)

    def test_day_number_is_clamped(self) -> None:
        self.assertIn("День 3 из 3", plan.day_reply(make_plan(), 99).text)
        self.assertIn("День 1 из 3", plan.day_reply(make_plan(), 0).text)

    def test_each_meal_has_recipe_and_replace_buttons(self) -> None:
        rows = plan.day_reply(make_plan(), 2).keyboard["inline_keyboard"]
        verbs = [parse_callback(button["callback_data"])[0] for row in rows for button in row]
        # §5.4: кнопки есть на каждом блюде любого дня, а не только сегодняшнего
        self.assertEqual(verbs.count("r"), 3)
        self.assertEqual(verbs.count("x"), 3)

    def test_navigation_between_days(self) -> None:
        rows = plan.day_reply(make_plan(), 2).keyboard["inline_keyboard"]
        nav = [button for row in rows for button in row
               if parse_callback(button["callback_data"])[0] == "d"]
        self.assertEqual([button["text"] for button in nav], ["◀", "▶"])
        self.assertEqual(parse_callback(nav[0]["callback_data"])[1][1], "1")
        self.assertEqual(parse_callback(nav[1]["callback_data"])[1][1], "3")

    def test_single_day_plan_has_no_navigation(self) -> None:
        rows = plan.day_reply(make_plan(days=1), 1).keyboard["inline_keyboard"]
        verbs = [parse_callback(button["callback_data"])[0] for row in rows for button in row]
        self.assertNotIn("d", verbs)

    def test_day_has_shopping_history_and_delete(self) -> None:
        rows = plan.day_reply(make_plan(), 1).keyboard["inline_keyboard"]
        labels = [button["text"] for row in rows for button in row]
        self.assertIn("🛒 Покупки", labels)
        self.assertIn("🗂 История", labels)
        self.assertIn("🗑 Удалить", labels)


class HistoryTests(unittest.TestCase):
    def _plans(self, count):
        return [{
            "id": str(uuid.uuid4()), "starts_on": f"2026-09-{index + 1:02d}",
            "days": 3, "estimated_cost_kop": 300000 + index,
        } for index in range(count)]

    def test_empty_history_offers_to_build(self) -> None:
        reply = run_async(plan.history_reply(_AppRepo(plans=[]), CONTEXT))
        self.assertIn("Планов пока нет", reply.text)
        self.assertIn("Составить меню", reply.keyboard["inline_keyboard"][0][0]["text"])

    def test_history_is_paginated_by_eight(self) -> None:
        reply = run_async(plan.history_reply(_AppRepo(plans=self._plans(20)), CONTEXT))
        rows = reply.keyboard["inline_keyboard"]
        openers = [row for row in rows
                   if parse_callback(row[0]["callback_data"])[0] == "d"]
        self.assertEqual(len(openers), 8)
        self.assertIn("20 шт", reply.text)

    def test_history_second_page_shows_next_plans(self) -> None:
        plans = self._plans(20)
        first = run_async(plan.history_reply(_AppRepo(plans=plans), CONTEXT, 1))
        second = run_async(plan.history_reply(_AppRepo(plans=plans), CONTEXT, 2))
        self.assertNotEqual(
            first.keyboard["inline_keyboard"][0][0]["callback_data"],
            second.keyboard["inline_keyboard"][0][0]["callback_data"],
        )

    def test_opening_from_history_carries_plan_id(self) -> None:
        plans = self._plans(2)
        reply = run_async(plan.history_reply(_AppRepo(plans=plans), CONTEXT))
        verb, parts = parse_callback(reply.keyboard["inline_keyboard"][0][0]["callback_data"])
        self.assertEqual((verb, parts[1]), ("d", "1"))


class DeleteTests(unittest.TestCase):
    def test_delete_removes_plan(self) -> None:
        from app.telegram.callbacks import pack_uuid

        app_repository = _AppRepo()
        result = run_async(plan.delete(app_repository, CONTEXT, pack_uuid(PLAN_ID)))
        self.assertEqual(app_repository.delete_calls, [PLAN_ID])
        self.assertIn("удалён", result.edit.text)

    def test_missing_plan_is_not_an_error(self) -> None:
        from app.telegram.callbacks import pack_uuid

        result = run_async(plan.delete(_AppRepo(deleted=False), CONTEXT, pack_uuid(PLAN_ID)))
        self.assertTrue(result.show_alert)

    def test_viewer_gets_permission_alert(self) -> None:
        from app.telegram.callbacks import pack_uuid

        class Viewer(_AppRepo):
            async def delete_plan(self, session, plan_id):
                raise PermissionError("Режим просмотра не позволяет удалять планы")

        result = run_async(plan.delete(Viewer(), CONTEXT, pack_uuid(PLAN_ID)))
        self.assertIn("Режим просмотра", result.toast)


class DispatchTests(unittest.TestCase):
    """Команды и кнопки доезжают до сцены через общий обработчик."""

    def test_new_command_opens_wizard(self) -> None:
        dialogs = _Dialogs()
        run_async(handle_message(
            _BotRepo(), USER_ID, "/new", TODAY,
            app_repository=_AppRepo(), dialogs=dialogs,
        ))
        self.assertEqual(dialogs.state.scene, plan.SCENE)

    def test_menu_button_shows_active_plan(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "📅 Меню", TODAY,
            app_repository=_AppRepo(plan=make_plan()), dialogs=_Dialogs(),
        ))
        self.assertIn("День 1 из 3", reply.text)

    def test_menu_without_plan_offers_wizard(self) -> None:
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "/plan", TODAY,
            app_repository=_AppRepo(plan=None), dialogs=_Dialogs(),
        ))
        self.assertIn("Плана пока нет", reply.text)

    def test_day_button_opens_that_day(self) -> None:
        from app.telegram.callbacks import pack_uuid

        result = run_async(handle_callback(
            _AppRepo(plan=make_plan()), _BotRepo(), USER_ID,
            encode_callback("d", pack_uuid(PLAN_ID), 2), TODAY, dialogs=_Dialogs(),
        ))
        self.assertIn("День 2 из 3", result.edit.text)

    def test_history_button_opens_history(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(plans=[]), _BotRepo(), USER_ID,
            encode_callback("p", "pl", 1), TODAY, dialogs=_Dialogs(),
        ))
        self.assertIn("Планов пока нет", result.edit.text)

    def test_stale_day_button_says_data_changed(self) -> None:
        from app.telegram.callbacks import pack_uuid

        result = run_async(handle_callback(
            _AppRepo(plan=None), _BotRepo(), USER_ID,
            encode_callback("d", pack_uuid(PLAN_ID), 1), TODAY, dialogs=_Dialogs(),
        ))
        self.assertTrue(result.show_alert)


if __name__ == "__main__":
    unittest.main()
