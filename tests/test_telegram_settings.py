"""Настройки семьи из чата (TZ-M7 T9, §5.10)."""

import asyncio
import os
import re
import sys
import unittest
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakes import FakePool, repository_with_pool

from app.telegram.callbacks import encode_callback, pack_uuid, parse_callback
from app.telegram.fsm import DialogState
from app.telegram.scenes import SceneContext, settings
from app.telegram.dispatch import handle_callback, handle_message
from app.web.categories import APPLIANCES, RULE_TYPES

USER_ID = 7
TODAY = date(2026, 8, 28)
PERSON_ID = uuid.uuid4()
RULE_ID = uuid.uuid4()
OWNER = {
    "user_id": str(uuid.uuid4()), "login": "tg7", "role": "owner",
    "household_id": str(uuid.uuid4()), "household_name": "Моя семья",
    "channel": "telegram",
}
VIEWER = {**OWNER, "role": "viewer"}


def run_async(coro):
    return asyncio.run(coro)


def profile(*, people=None, appliances=None, rules=None):
    return {
        "user": {"id": OWNER["user_id"], "login": "tg7", "has_password": False},
        "household": {"id": OWNER["household_id"], "name": "Моя семья", "role": "owner"},
        "people": people if people is not None else [
            {"id": str(PERSON_ID), "name": "Я", "person_type": "adult",
             "target_kcal": 2000, "portion_factor": 1},
        ],
        "appliances": appliances if appliances is not None else ["stove", "oven"],
        "dietary_rules": rules if rules is not None else [
            {"id": str(RULE_ID), "rule_type": "allergy", "term": "орехи", "is_hard": True},
        ],
        "telegram_linked": True,
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
    def __init__(self, *, data=None, error=None, has_password=False):
        self.data = data or profile()
        self.error = error
        self._has_password = has_password
        self.appliance_calls = []
        self.person_calls = []
        self.removed_people = []
        self.rule_calls = []
        self.removed_rules = []
        self.renames = []

    async def get_profile(self, session):
        return self.data

    async def dashboard(self, session):
        return {"recipes": 3130, "recipes_ready": 983, "sources": 37,
                "products": 14656, "inventory": 5}

    async def has_password(self, user_id):
        return self._has_password

    async def update_appliances(self, session, codes):
        if self.error:
            raise self.error
        self.appliance_calls.append(list(codes))
        self.data = {**self.data, "appliances": list(codes)}
        return list(codes)

    async def add_person(self, session, person):
        if self.error:
            raise self.error
        self.person_calls.append(person)
        return {**person, "id": str(uuid.uuid4())}

    async def delete_person(self, session, person_id):
        if self.error:
            raise self.error
        self.removed_people.append(person_id)
        return True

    async def add_dietary_rule(self, session, rule):
        if self.error:
            raise self.error
        self.rule_calls.append(rule)
        return {**rule, "id": str(uuid.uuid4())}

    async def delete_dietary_rule(self, session, rule_id):
        if self.error:
            raise self.error
        self.removed_rules.append(rule_id)
        return True

    async def rename_household(self, session, name):
        if self.error:
            raise self.error
        self.renames.append(name)
        return name.strip()

    async def web_login_code(self, user_id):
        return "123456"


class _BotRepo:
    async def context_for_user(self, user_id):
        return OWNER

    async def latest_plan_meals(self, household_id):
        return []

    async def shopping_items(self, household_id):
        return []


def buttons(reply):
    return [b for row in reply.keyboard["inline_keyboard"] for b in row]


def ctx(text, state, app_repository, dialogs, session=OWNER):
    return SceneContext(
        actor=type("A", (), {"user_id": USER_ID, "chat_id": USER_ID})(),
        text=text, state=state, bot_repository=_BotRepo(),
        app_repository=app_repository, dialogs=dialogs, today=TODAY, session=dict(session),
    )


class LabelDriftTests(unittest.TestCase):
    def _js_map(self, name):
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "web", "static", "js", "format.js"
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        block = re.search(rf"{name} = \{{(.*?)\n\}};", source, re.S).group(1)
        return dict(re.findall(r'(\w+):\s*"([^"]+)"', block))

    def test_appliances_match_js(self) -> None:
        self.assertEqual(self._js_map("APPLIANCES"), APPLIANCES)

    def test_rule_types_match_js(self) -> None:
        self.assertEqual(self._js_map("RULE_TYPES"), RULE_TYPES)


class MenuTests(unittest.TestCase):
    def test_menu_shows_household_and_role(self) -> None:
        reply = settings.menu_reply(OWNER)
        self.assertIn("Моя семья", reply.text)
        self.assertIn("владелец", reply.text)
        labels = [b["text"] for b in buttons(reply)]
        self.assertIn("🍳 Техника", labels)
        self.assertIn("📊 Данные", labels)

    def test_viewer_is_warned_about_rights(self) -> None:
        self.assertIn("владелец и администратор", settings.menu_reply(VIEWER).text)

    def test_begin_opens_scene(self) -> None:
        dialogs = _Dialogs()
        run_async(settings.begin(dialogs, OWNER, USER_ID))
        self.assertEqual(dialogs.state.scene, settings.SCENE)


class FamilyTests(unittest.TestCase):
    def test_people_are_listed(self) -> None:
        reply = run_async(settings.family_reply(_AppRepo(), OWNER))
        self.assertIn("Я — взрослый, 2000 ккал", reply.text)

    def test_person_without_target_says_so(self) -> None:
        data = profile(people=[{"id": str(PERSON_ID), "name": "Маша",
                                "person_type": "child", "target_kcal": None,
                                "portion_factor": 0.7}])
        reply = run_async(settings.family_reply(_AppRepo(data=data), OWNER))
        self.assertIn("норма не задана", reply.text)

    def test_viewer_sees_no_edit_buttons(self) -> None:
        reply = run_async(settings.family_reply(_AppRepo(), VIEWER))
        labels = [b["text"] for b in buttons(reply)]
        self.assertNotIn("➕ Добавить", labels)
        self.assertEqual(labels, ["◀ К настройкам"])

    def test_add_person_walks_three_steps(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(settings.SCENE, "menu", {}))
        run_async(settings.handle_navigation(
            app_repository, dialogs, OWNER, USER_ID, ["st", "padd"]
        ))
        self.assertEqual(dialogs.state.step, "person_name")

        run_async(settings.handle_step(ctx("Маша", dialogs.state, app_repository, dialogs)))
        self.assertEqual(dialogs.state.step, "person_type")
        self.assertEqual(dialogs.state.data["name"], "Маша")

        run_async(settings.handle_navigation(
            app_repository, dialogs, OWNER, USER_ID, ["st", "ptype", "child"]
        ))
        self.assertEqual(dialogs.state.step, "person_kcal")

        run_async(settings.handle_step(ctx("1500", dialogs.state, app_repository, dialogs)))
        self.assertEqual(app_repository.person_calls[0]["name"], "Маша")
        self.assertEqual(app_repository.person_calls[0]["person_type"], "child")
        self.assertEqual(app_repository.person_calls[0]["target_kcal"], 1500)

    def test_kcal_can_be_skipped(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(
            settings.SCENE, "person_kcal", {"name": "Петя", "person_type": "adult"}
        ))
        run_async(settings.handle_navigation(
            app_repository, dialogs, OWNER, USER_ID, ["st", "pkcal", "0"]
        ))
        self.assertIsNone(app_repository.person_calls[0]["target_kcal"])

    def test_kcal_out_of_range_repeats(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(settings.SCENE, "person_kcal", {"name": "Петя"}))
        reply = run_async(settings.handle_step(
            ctx("100", dialogs.state, app_repository, dialogs)
        ))
        self.assertEqual(app_repository.person_calls, [])
        self.assertIn("от 500 до 6000", reply.text)

    def test_empty_name_repeats(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(settings.SCENE, "person_name", {}))
        reply = run_async(settings.handle_step(ctx("", dialogs.state, app_repository, dialogs)))
        self.assertIn("от 1 до 80", reply.text)

    def test_remove_person(self) -> None:
        app_repository = _AppRepo()
        run_async(settings.delete_person(app_repository, OWNER, pack_uuid(PERSON_ID)))
        self.assertEqual(app_repository.removed_people, [PERSON_ID])

    def test_last_person_cannot_be_removed(self) -> None:
        app_repository = _AppRepo(
            error=ValueError("В семье должен остаться хотя бы один человек")
        )
        result = run_async(settings.delete_person(app_repository, OWNER, pack_uuid(PERSON_ID)))
        self.assertTrue(result.show_alert)
        self.assertIn("хотя бы один", result.toast)

    def test_rename_household(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(settings.SCENE, "rename", {}))
        run_async(settings.handle_step(
            ctx("Семья Ивановых", dialogs.state, app_repository, dialogs)
        ))
        self.assertEqual(app_repository.renames, ["Семья Ивановых"])
        self.assertEqual(dialogs.state.step, "menu")


class AppliancesTests(unittest.TestCase):
    def test_all_thirteen_are_shown_with_marks(self) -> None:
        reply = run_async(settings.appliances_reply(_AppRepo(), OWNER))
        marks = [b["text"] for b in buttons(reply) if b["text"][0] in "✅☐"]
        self.assertEqual(len(marks), len(APPLIANCES))
        self.assertTrue(any(label.startswith("✅ Плита") for label in marks))
        self.assertTrue(any(label.startswith("☐ Блендер") for label in marks))
        self.assertIn("отмечено 2 из 13", reply.text)

    def test_toggle_adds_and_removes(self) -> None:
        app_repository = _AppRepo()
        run_async(settings.toggle_appliance(app_repository, OWNER, "blender"))
        self.assertEqual(app_repository.appliance_calls[0], ["blender", "oven", "stove"])
        run_async(settings.toggle_appliance(app_repository, OWNER, "oven"))
        self.assertEqual(app_repository.appliance_calls[1], ["blender", "stove"])

    def test_unknown_code_is_rejected(self) -> None:
        result = run_async(settings.toggle_appliance(_AppRepo(), OWNER, "телепорт"))
        self.assertEqual(result.toast, "Не понял кнопку.")

    def test_viewer_gets_alert(self) -> None:
        app_repository = _AppRepo(error=PermissionError("Недостаточно прав"))
        result = run_async(settings.toggle_appliance(app_repository, VIEWER, "grill"))
        self.assertTrue(result.show_alert)


class RulesTests(unittest.TestCase):
    def test_rules_are_listed(self) -> None:
        reply = run_async(settings.rules_reply(_AppRepo(), OWNER))
        self.assertIn("Аллергия · орехи · строгое", reply.text)

    def test_empty_rules(self) -> None:
        reply = run_async(settings.rules_reply(_AppRepo(data=profile(rules=[])), OWNER))
        self.assertIn("Пока ни одного", reply.text)

    def test_add_rule_walks_three_steps(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(settings.SCENE, "menu", {}))
        run_async(settings.handle_navigation(
            app_repository, dialogs, OWNER, USER_ID, ["st", "radd"]
        ))
        types = run_async(settings.handle_navigation(
            app_repository, dialogs, OWNER, USER_ID, ["st", "rtype", "allergy"]
        ))
        self.assertIn("на какой продукт", types.edit.text)
        run_async(settings.handle_step(ctx("орехи", dialogs.state, app_repository, dialogs)))
        self.assertEqual(dialogs.state.step, "rule_hard")
        run_async(settings.handle_navigation(
            app_repository, dialogs, OWNER, USER_ID, ["st", "rhard", "1"]
        ))
        self.assertEqual(app_repository.rule_calls[0],
                         {"rule_type": "allergy", "term": "орехи", "is_hard": True})

    def test_soft_rule(self) -> None:
        app_repository = _AppRepo()
        dialogs = _Dialogs(DialogState(
            settings.SCENE, "rule_hard", {"rule_type": "dislike", "term": "рыба"}
        ))
        run_async(settings.handle_navigation(
            app_repository, dialogs, OWNER, USER_ID, ["st", "rhard", "0"]
        ))
        self.assertFalse(app_repository.rule_calls[0]["is_hard"])

    def test_unknown_rule_type(self) -> None:
        result = run_async(settings.handle_navigation(
            _AppRepo(), _Dialogs(), OWNER, USER_ID, ["st", "rtype", "чепуха"]
        ))
        self.assertEqual(result.toast, "Не понял кнопку.")

    def test_remove_rule(self) -> None:
        app_repository = _AppRepo()
        run_async(settings.delete_rule(app_repository, OWNER, pack_uuid(RULE_ID)))
        self.assertEqual(app_repository.removed_rules, [RULE_ID])

    def test_viewer_cannot_open_add_form(self) -> None:
        result = run_async(settings.handle_navigation(
            _AppRepo(), _Dialogs(), VIEWER, USER_ID, ["st", "radd"]
        ))
        self.assertTrue(result.show_alert)
        self.assertIn("владелец и администратор", result.toast)


class TelegramAndDataTests(unittest.TestCase):
    def test_telegram_panel_without_password(self) -> None:
        reply = settings.telegram_reply(OWNER, has_password=False)
        self.assertIn("Пароля нет", reply.text)
        self.assertIn("🌐 Войти в веб", [b["text"] for b in buttons(reply)])

    def test_telegram_panel_with_password(self) -> None:
        self.assertIn("Пароль задан", settings.telegram_reply(OWNER, True).text)

    def test_web_login_available_to_viewer(self) -> None:
        """Вход в свой браузер — не настройка семьи, роль тут ни при чём."""
        result = run_async(settings.handle_navigation(
            _AppRepo(), _Dialogs(), VIEWER, USER_ID, ["st", "web"]
        ))
        self.assertIn("123456", result.edit.text)

    def test_data_counters(self) -> None:
        reply = run_async(settings.data_reply(_AppRepo(), OWNER))
        self.assertIn("3130", reply.text)
        self.assertIn("14656", reply.text)


class DispatchTests(unittest.TestCase):
    def test_settings_button_opens_menu(self) -> None:
        dialogs = _Dialogs()
        reply = run_async(handle_message(
            _BotRepo(), USER_ID, "⚙️ Настройки", TODAY,
            app_repository=_AppRepo(), dialogs=dialogs,
        ))
        self.assertEqual(dialogs.state.scene, settings.SCENE)
        self.assertIn("Настройки семьи", reply.text)

    def test_appliance_toggle_through_dispatch(self) -> None:
        app_repository = _AppRepo()
        run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID,
            encode_callback("o", "ap", "grill"), TODAY, dialogs=_Dialogs(),
        ))
        self.assertIn("grill", app_repository.appliance_calls[0])

    def test_person_removal_through_dispatch(self) -> None:
        app_repository = _AppRepo()
        run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID,
            encode_callback("y", "sp", pack_uuid(PERSON_ID)), TODAY, dialogs=_Dialogs(),
        ))
        self.assertEqual(app_repository.removed_people, [PERSON_ID])

    def test_rule_removal_through_dispatch(self) -> None:
        app_repository = _AppRepo()
        run_async(handle_callback(
            app_repository, _BotRepo(), USER_ID,
            encode_callback("y", "sr", pack_uuid(RULE_ID)), TODAY, dialogs=_Dialogs(),
        ))
        self.assertEqual(app_repository.removed_rules, [RULE_ID])

    def test_submenu_through_dispatch(self) -> None:
        result = run_async(handle_callback(
            _AppRepo(), _BotRepo(), USER_ID,
            encode_callback("n", "st", "appl"), TODAY, dialogs=_Dialogs(),
        ))
        self.assertIn("Техника", result.edit.text)

    def test_every_button_fits_in_64_bytes(self) -> None:
        screens = [
            settings.menu_reply(OWNER),
            run_async(settings.family_reply(_AppRepo(), OWNER)),
            run_async(settings.appliances_reply(_AppRepo(), OWNER)),
            run_async(settings.rules_reply(_AppRepo(), OWNER)),
            settings.telegram_reply(OWNER, False),
            settings.ask_rule_type(),
            settings.ask_person_type("Маша"),
        ]
        for reply in screens:
            for button in buttons(reply):
                self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)


class RepositorySqlTests(unittest.TestCase):
    """Точечные методы вместо полного save_settings (§5.10)."""

    def test_rename_requires_admin(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        with self.assertRaises(PermissionError):
            run_async(repository.rename_household({**OWNER, "role": "editor"}, "Дом"))
        self.assertEqual(pool.calls, [])

    def test_rename_validates_length(self) -> None:
        with self.assertRaises(ValueError):
            run_async(repository_with_pool(FakePool()).rename_household(OWNER, "  "))

    def test_update_appliances_replaces_only_appliances(self) -> None:
        pool = FakePool()
        run_async(repository_with_pool(pool).update_appliances(OWNER, ["oven", "stove", "oven"]))
        delete_sql, _ = pool.first_matching("DELETE FROM app_core.appliances")
        self.assertIn("household_id=$1", delete_sql)
        _, args = pool.first_matching("INSERT INTO app_core.appliances")
        # дубли схлопнуты, порядок стабильный
        self.assertEqual([pair[1] for pair in args[0]], ["oven", "stove"])
        self.assertEqual(pool.count_matching("DELETE FROM app_core.people"), 0)
        self.assertEqual(pool.count_matching("DELETE FROM app_core.dietary_rules"), 0)

    def test_add_person_takes_next_position(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "INSERT INTO app_core.people", {
            "id": PERSON_ID, "name": "Маша", "person_type": "child",
            "target_kcal": 1500, "portion_factor": 1, "position": 2,
        })
        run_async(repository_with_pool(pool).add_person(
            OWNER, {"name": "Маша", "person_type": "child", "target_kcal": 1500}
        ))
        sql, _ = pool.first_matching("INSERT INTO app_core.people")
        self.assertIn("MAX(p.position), 0) + 1", sql)

    def test_add_person_validates_name(self) -> None:
        with self.assertRaises(ValueError):
            run_async(repository_with_pool(FakePool()).add_person(OWNER, {"name": ""}))

    def test_delete_last_person_is_refused(self) -> None:
        pool = FakePool()
        pool.on("fetchval", "SELECT count(*) FROM app_core.people", 1)
        with self.assertRaises(ValueError):
            run_async(repository_with_pool(pool).delete_person(OWNER, PERSON_ID))
        self.assertEqual(pool.count_matching("DELETE FROM app_core.people"), 0)

    def test_delete_person_scoped_to_household(self) -> None:
        pool = FakePool()
        pool.on("fetchval", "SELECT count(*) FROM app_core.people", 3)
        self.assertTrue(run_async(repository_with_pool(pool).delete_person(OWNER, PERSON_ID)))
        sql, args = pool.first_matching("DELETE FROM app_core.people")
        self.assertIn("household_id=$2", sql)
        self.assertEqual(args, (PERSON_ID, OWNER["household_id"]))

    def test_add_rule_validates_term(self) -> None:
        with self.assertRaises(ValueError):
            run_async(repository_with_pool(FakePool()).add_dietary_rule(OWNER, {"term": ""}))

    def test_delete_rule_reports_missing(self) -> None:
        pool = FakePool()
        pool.default_execute = "DELETE 0"
        self.assertFalse(
            run_async(repository_with_pool(pool).delete_dietary_rule(OWNER, RULE_ID))
        )

    def test_appliance_audit_carries_channel(self) -> None:
        pool = FakePool()
        run_async(repository_with_pool(pool).update_appliances(OWNER, ["stove"]))
        _, args = pool.first_matching("INSERT INTO app_core.audit_log")
        self.assertIn("telegram", args)


if __name__ == "__main__":
    unittest.main()
