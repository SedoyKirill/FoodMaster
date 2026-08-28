"""Настройки семьи из чата: люди, техника, ограничения (TZ-M7 §5.10).

Правки идут точечно — по одному полю за раз. Полный ``save_settings`` требует
прислать людей, технику и правила целиком, и два канала (браузер и бот) начали
бы затирать изменения друг друга.

Что сюда ещё не приехало: «🎯 Как планируем» и «😋 Вкусы» — это профиль
планирования и модель вкуса из TZ-M8, их таблиц пока нет. Тема оформления —
осознанное исключение: в Telegram ей нет аналога.
"""

from __future__ import annotations

from typing import Any

from app.web.categories import APPLIANCES, RULE_TYPES

from ..callbacks import encode_callback, pack_uuid, unpack_uuid
from ..fsm import CANCEL_BUTTON, DialogState
from ..render import CallbackReply, Reply, build_keyboard, button_text
from . import SceneContext, auth

SCENE = "settings.edit"

PERSON_TYPES = {"adult": "Взрослый", "child": "Ребёнок"}

MENU_TEXT = "⚙️ Настройки семьи «{household}» · вы {role}."

ROLE_LABELS = {
    "owner": "владелец", "admin": "администратор",
    "editor": "редактор", "viewer": "наблюдатель",
}

NO_RIGHTS_TEXT = "Менять настройки могут владелец и администратор."


def _can_edit(session: dict[str, Any]) -> bool:
    return session.get("role") in {"owner", "admin"}


# --- главное меню --------------------------------------------------------------

def menu_reply(session: dict[str, Any]) -> Reply:
    rows = [
        [{"text": "👨‍👩‍👧 Семья", "callback_data": encode_callback("n", "st", "family")},
         {"text": "🍳 Техника", "callback_data": encode_callback("n", "st", "appl")}],
        [{"text": "🚫 Ограничения", "callback_data": encode_callback("n", "st", "rules")},
         {"text": "🔗 Телеграм", "callback_data": encode_callback("n", "st", "tg")}],
        [{"text": "📊 Данные", "callback_data": encode_callback("n", "st", "data")}],
    ]
    text = MENU_TEXT.format(
        household=session.get("household_name") or "—",
        role=ROLE_LABELS.get(str(session.get("role")), session.get("role")),
    )
    if not _can_edit(session):
        text += f"\n\n{NO_RIGHTS_TEXT}"
    return Reply(text, build_keyboard(rows))


async def begin(dialogs: Any, session: dict[str, Any], user_id: int) -> Reply:
    await dialogs.save(user_id, DialogState(SCENE, "menu", {}))
    return menu_reply(session)


def _back_row() -> list[dict[str, Any]]:
    return [{"text": "◀ К настройкам", "callback_data": encode_callback("n", "st", "menu")}]


# --- семья ---------------------------------------------------------------------

def _person_line(person: dict[str, Any]) -> str:
    kind = PERSON_TYPES.get(str(person.get("person_type")), "взрослый")
    kcal = person.get("target_kcal")
    kcal_text = f"{kcal} ккал" if kcal else "норма не задана"
    portion = person.get("portion_factor")
    return f"• {person.get('name')} — {kind.lower()}, {kcal_text}, порция ×{portion}"


async def family_reply(app_repository: Any, session: dict[str, Any]) -> Reply:
    profile = await app_repository.get_profile(session)
    people = profile.get("people") or []
    lines = [f"👨‍👩‍👧 Семья «{profile['household']['name']}» — {len(people)} чел."]
    lines += [_person_line(person) for person in people]
    rows = []
    if _can_edit(session):
        rows = [[{
            "text": button_text(f"🗑 {person.get('name')}"),
            "callback_data": encode_callback("y", "sp", pack_uuid(person["id"])),
        }] for person in people]
        rows.append([
            {"text": "➕ Добавить", "callback_data": encode_callback("n", "st", "padd")},
            {"text": "✏️ Переименовать семью",
             "callback_data": encode_callback("n", "st", "rename")},
        ])
    rows.append(_back_row())
    return Reply("\n".join(lines), build_keyboard(rows))


# --- техника -------------------------------------------------------------------

async def appliances_reply(app_repository: Any, session: dict[str, Any]) -> Reply:
    profile = await app_repository.get_profile(session)
    chosen = set(profile.get("appliances") or [])
    codes = list(APPLIANCES)
    rows = [[{
        "text": button_text(f"{'✅' if code in chosen else '☐'} {APPLIANCES[code]}"),
        "callback_data": encode_callback("o", "ap", code),
    } for code in codes[index:index + 2]] for index in range(0, len(codes), 2)]
    rows.append(_back_row())
    text = (
        f"🍳 Техника — отмечено {len(chosen)} из {len(APPLIANCES)}.\n"
        "Планировщик не предложит блюдо, для которого нет техники."
    )
    if not _can_edit(session):
        text += f"\n\n{NO_RIGHTS_TEXT}"
    return Reply(text, build_keyboard(rows))


async def toggle_appliance(app_repository: Any, session: dict[str, Any],
                           code: str) -> CallbackReply:
    if code not in APPLIANCES:
        return CallbackReply(toast="Не понял кнопку.")
    profile = await app_repository.get_profile(session)
    chosen = set(profile.get("appliances") or [])
    if code in chosen:
        chosen.discard(code)
    else:
        chosen.add(code)
    try:
        await app_repository.update_appliances(session, sorted(chosen))
    except PermissionError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    return CallbackReply(edit=await appliances_reply(app_repository, session))


# --- ограничения ----------------------------------------------------------------

def _rule_line(rule: dict[str, Any]) -> str:
    kind = RULE_TYPES.get(str(rule.get("rule_type")), str(rule.get("rule_type")))
    strict = "строгое" if rule.get("is_hard") else "мягкое"
    return f"• {kind} · {rule.get('term')} · {strict}"


async def rules_reply(app_repository: Any, session: dict[str, Any]) -> Reply:
    profile = await app_repository.get_profile(session)
    rules = profile.get("dietary_rules") or []
    lines = ["🚫 Ограничения в питании."]
    lines += [_rule_line(rule) for rule in rules] or ["Пока ни одного."]
    lines.append("")
    lines.append("Строгое правило исключает рецепт целиком, мягкое — понижает его в выдаче.")
    rows = []
    if _can_edit(session):
        rows = [[{
            "text": button_text(f"🗑 {rule.get('term')}"),
            "callback_data": encode_callback("y", "sr", pack_uuid(rule["id"])),
        }] for rule in rules]
        rows.append([{"text": "➕ Добавить правило",
                      "callback_data": encode_callback("n", "st", "radd")}])
    rows.append(_back_row())
    return Reply("\n".join(lines), build_keyboard(rows))


# --- телеграм и данные -----------------------------------------------------------

def telegram_reply(session: dict[str, Any], has_password: bool) -> Reply:
    lines = [f"🔗 Telegram привязан к аккаунту «{session.get('login')}»."]
    lines.append(
        "Пароль задан — в браузер можно войти и без бота."
        if has_password else
        "Пароля нет: в браузер входите по коду из /web."
    )
    rows = [
        [{"text": "🌐 Войти в веб", "callback_data": encode_callback("n", "st", "web")}],
        [{"text": "🔓 Отвязать", "callback_data": encode_callback("n", "st", "unlink")}],
        _back_row(),
    ]
    return Reply("\n".join(lines), build_keyboard(rows))


async def data_reply(app_repository: Any, session: dict[str, Any]) -> Reply:
    counters = await app_repository.dashboard(session)
    lines = [
        "📊 Данные:",
        f"• рецептов в библиотеке: {counters.get('recipes', 0)}",
        f"• из них проверено: {counters.get('recipes_ready', 0)}",
        f"• источников обработано: {counters.get('sources', 0)}",
        f"• товаров «Ленты»: {counters.get('products', 0)}",
        f"• партий дома: {counters.get('inventory', 0)}",
    ]
    return Reply("\n".join(lines), build_keyboard([_back_row()]))


# --- сцены добавления ------------------------------------------------------------

def ask_person_name() -> Reply:
    return Reply("Как зовут человека?", build_keyboard([[CANCEL_BUTTON]]))


def ask_person_type(name: str) -> Reply:
    rows = [[{
        "text": label,
        "callback_data": encode_callback("n", "st", "ptype", code),
    } for code, label in PERSON_TYPES.items()], [CANCEL_BUTTON]]
    return Reply(f"«{name}» — взрослый или ребёнок?", build_keyboard(rows))


def ask_person_kcal(name: str) -> Reply:
    rows = [[{
        "text": "Не задавать",
        "callback_data": encode_callback("n", "st", "pkcal", 0),
    }], [CANCEL_BUTTON]]
    return Reply(
        f"Дневная норма для «{name}» в ккал? Напишите число от 500 до 6000 "
        "или пропустите.",
        build_keyboard(rows),
    )


def ask_rule_type() -> Reply:
    rows = [[{
        "text": label,
        "callback_data": encode_callback("n", "st", "rtype", code),
    }] for code, label in RULE_TYPES.items()]
    rows.append([CANCEL_BUTTON])
    return Reply("Какое это ограничение?", build_keyboard(rows))


def ask_rule_term(kind: str) -> Reply:
    return Reply(
        f"{RULE_TYPES.get(kind, kind)}: на какой продукт? Напишите одно слово, "
        "например «орехи».",
        build_keyboard([[CANCEL_BUTTON]]),
    )


def ask_rule_strict(term: str) -> Reply:
    rows = [[
        {"text": "Строгое", "callback_data": encode_callback("n", "st", "rhard", 1)},
        {"text": "Мягкое", "callback_data": encode_callback("n", "st", "rhard", 0)},
    ], [CANCEL_BUTTON]]
    return Reply(
        f"«{term}»: исключать рецепт совсем или только понижать в выдаче?",
        build_keyboard(rows),
    )


# --- шаги свободного текста -------------------------------------------------------

async def handle_step(ctx: SceneContext) -> Reply:
    """Текст в сцене настроек: имя, ккал, название семьи или продукт."""
    session = ctx.session or {}
    data = dict(ctx.state.data or {})
    step = ctx.state.step
    text = (ctx.text or "").strip()

    if step == "rename":
        try:
            name = await ctx.app_repository.rename_household(session, text)
        except (PermissionError, ValueError) as exc:
            return Reply(str(exc), build_keyboard([[CANCEL_BUTTON]]))
        session["household_name"] = name
        await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "menu", {}))
        return await family_reply(ctx.app_repository, session)

    if step == "person_name":
        if not 1 <= len(text) <= 80:
            return Reply("Имя: от 1 до 80 символов.", build_keyboard([[CANCEL_BUTTON]]))
        data["name"] = text
        await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "person_type", data))
        return ask_person_type(text)

    if step == "person_kcal":
        digits = "".join(char for char in text if char.isdigit())
        if not digits or not 500 <= int(digits) <= 6000:
            return Reply("Норма: число от 500 до 6000 ккал.",
                         ask_person_kcal(data.get("name", "")).keyboard)
        data["target_kcal"] = int(digits)
        return await _store_person(ctx, data)

    if step == "rule_term":
        if not 1 <= len(text) <= 100:
            return Reply("Продукт: от 1 до 100 символов.",
                         build_keyboard([[CANCEL_BUTTON]]))
        data["term"] = text
        await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "rule_hard", data))
        return ask_rule_strict(text)

    return menu_reply(session)


async def _store_person(ctx: SceneContext, data: dict[str, Any]) -> Reply:
    session = ctx.session or {}
    try:
        await ctx.app_repository.add_person(session, data)
    except (PermissionError, ValueError) as exc:
        return Reply(str(exc))
    await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "menu", {}))
    return await family_reply(ctx.app_repository, session)


async def _store_rule(app_repository: Any, dialogs: Any, session: dict[str, Any],
                      user_id: int, data: dict[str, Any]) -> Reply:
    try:
        await app_repository.add_dietary_rule(session, data)
    except (PermissionError, ValueError) as exc:
        return Reply(str(exc))
    await dialogs.save(user_id, DialogState(SCENE, "menu", {}))
    return await rules_reply(app_repository, session)


# --- кнопки ------------------------------------------------------------------------

async def handle_navigation(app_repository: Any, dialogs: Any, session: dict[str, Any],
                            user_id: int, parts: list[str]) -> CallbackReply | None:
    """Глагол ``n`` для настроек: подменю и шаги добавления."""
    if parts[:1] != ["st"] or len(parts) < 2:
        return None
    action = parts[1]
    value = parts[2] if len(parts) > 2 else ""

    if action == "menu":
        await dialogs.save(user_id, DialogState(SCENE, "menu", {}))
        return CallbackReply(edit=menu_reply(session))
    if action == "family":
        return CallbackReply(edit=await family_reply(app_repository, session))
    if action == "appl":
        return CallbackReply(edit=await appliances_reply(app_repository, session))
    if action == "rules":
        return CallbackReply(edit=await rules_reply(app_repository, session))
    if action == "data":
        return CallbackReply(edit=await data_reply(app_repository, session))
    if action == "tg":
        has_password = await app_repository.has_password(session["user_id"])
        return CallbackReply(edit=telegram_reply(session, has_password))
    if action == "web":
        # свой аккаунт — не настройка семьи, роль тут ни при чём
        return CallbackReply(edit=await auth.web_login(app_repository, session))
    if action == "unlink":
        has_password = await app_repository.has_password(session["user_id"])
        return CallbackReply(edit=auth.unlink_confirmation(has_password))

    if not _can_edit(session):
        return CallbackReply(toast=NO_RIGHTS_TEXT, show_alert=True)

    if action == "rename":
        await dialogs.save(user_id, DialogState(SCENE, "rename", {}))
        return CallbackReply(edit=Reply("Новое название семьи?",
                                        build_keyboard([[CANCEL_BUTTON]])))
    if action == "padd":
        await dialogs.save(user_id, DialogState(SCENE, "person_name", {}))
        return CallbackReply(edit=ask_person_name())
    if action == "ptype":
        state = await dialogs.load(user_id)
        data = dict((state.data if state else {}) or {})
        data["person_type"] = "child" if value == "child" else "adult"
        await dialogs.save(user_id, DialogState(SCENE, "person_kcal", data))
        return CallbackReply(edit=ask_person_kcal(data.get("name", "")))
    if action == "pkcal":
        state = await dialogs.load(user_id)
        data = dict((state.data if state else {}) or {})
        data["target_kcal"] = None
        try:
            await app_repository.add_person(session, data)
        except (PermissionError, ValueError) as exc:
            return CallbackReply(toast=str(exc), show_alert=True)
        await dialogs.save(user_id, DialogState(SCENE, "menu", {}))
        return CallbackReply(edit=await family_reply(app_repository, session))

    if action == "radd":
        await dialogs.save(user_id, DialogState(SCENE, "rule_type", {}))
        return CallbackReply(edit=ask_rule_type())
    if action == "rtype":
        if value not in RULE_TYPES:
            return CallbackReply(toast="Не понял кнопку.")
        await dialogs.save(user_id, DialogState(SCENE, "rule_term", {"rule_type": value}))
        return CallbackReply(edit=ask_rule_term(value))
    if action == "rhard":
        state = await dialogs.load(user_id)
        data = dict((state.data if state else {}) or {})
        data["is_hard"] = value == "1"
        reply = await _store_rule(app_repository, dialogs, session, user_id, data)
        return CallbackReply(edit=reply)
    return None


async def delete_person(app_repository: Any, session: dict[str, Any],
                        packed: str) -> CallbackReply:
    person_id = unpack_uuid(packed)
    if person_id is None:
        return CallbackReply(toast="Не понял кнопку.")
    try:
        removed = await app_repository.delete_person(session, person_id)
    except (PermissionError, ValueError) as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    if not removed:
        return CallbackReply(toast="Этого человека уже убрали.", show_alert=True)
    return CallbackReply(edit=await family_reply(app_repository, session))


async def delete_rule(app_repository: Any, session: dict[str, Any],
                      packed: str) -> CallbackReply:
    rule_id = unpack_uuid(packed)
    if rule_id is None:
        return CallbackReply(toast="Не понял кнопку.")
    try:
        removed = await app_repository.delete_dietary_rule(session, rule_id)
    except PermissionError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    if not removed:
        return CallbackReply(toast="Это правило уже убрали.", show_alert=True)
    return CallbackReply(edit=await rules_reply(app_repository, session))

