"""Настройки семьи из чата: люди, техника, ограничения (TZ-M7 §5.10).

Правки идут точечно — по одному полю за раз. Полный ``save_settings`` требует
прислать людей, технику и правила целиком, и два канала (браузер и бот) начали
бы затирать изменения друг друга.

Что сюда ещё не приехало: «🎯 Как планируем» — профиль планирования из
TZ-M8. «😋 Вкусы» живут в ``scenes/taste.py`` и появляются в меню сами, когда
модель вкуса доедет до репозитория. Тема оформления — осознанное исключение:
в Telegram ей нет аналога.
"""

from __future__ import annotations

from typing import Any

from app.web.categories import APPLIANCES, RULE_TYPES

from .. import notifications
from ..callbacks import encode_callback, pack_uuid, unpack_uuid
from ..fsm import CANCEL_BUTTON, DialogState
from ..render import CallbackReply, Reply, build_keyboard, button_text
from . import SceneContext, auth
from . import taste as taste_scene

SCENE = "settings.edit"

PERSON_TYPES = {"adult": "Взрослый", "child": "Ребёнок"}

#: Режимы планирования (TZ-M8 §6.4) — те же пять, что в мастере меню и в вебе.
PLAN_MODES = {
    "balanced": "⚖️ Сбалансированно",
    "economy": "💰 Экономно",
    "variety": "🎲 Разнообразно",
    "fitness": "💪 Фитнес",
    "quick": "⚡ Быстро",
}
#: сколько нового в меню (§4.5): доля слотов под непробованные блюда
NOVELTY_LEVELS = {"low": "Проверенное", "medium": "Поровну", "high": "Больше нового"}
CUISINE_MODES = {"only": "только выбранные", "prefer": "предпочитать"}
MEAL_NAMES = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}
#: горизонты, которые предлагаются кнопками; свободного ввода тут нет
DAY_CHOICES = (3, 5, 7, 14)
#: сколько раз одно блюдо повторяется за план (CHECK в схеме — 1..7)
REPEAT_CHOICES = (1, 2, 3, 4)
#: лимиты времени готовки: поле профиля → как называется и в какой шаг ведёт
TIME_LIMITS = {
    "weekday_max_minutes": ("Будни", "plan_wd"),
    "weekend_max_minutes": ("Выходные", "plan_we"),
    "breakfast_max_minutes": ("Завтрак", "plan_bf"),
}

MENU_TEXT = "⚙️ Настройки семьи «{household}» · вы {role}."

ROLE_LABELS = {
    "owner": "владелец", "admin": "администратор",
    "editor": "редактор", "viewer": "наблюдатель",
}

NO_RIGHTS_TEXT = "Менять настройки могут владелец и администратор."


def _can_edit(session: dict[str, Any]) -> bool:
    return session.get("role") in {"owner", "admin"}


# --- главное меню --------------------------------------------------------------

def menu_reply(session: dict[str, Any], taste_ready: bool = False) -> Reply:
    rows = [
        [{"text": "👨‍👩‍👧 Семья", "callback_data": encode_callback("n", "st", "family")},
         {"text": "🍳 Техника", "callback_data": encode_callback("n", "st", "appl")}],
        [{"text": "🚫 Ограничения", "callback_data": encode_callback("n", "st", "rules")},
         {"text": "🔗 Телеграм", "callback_data": encode_callback("n", "st", "tg")}],
        [{"text": "🔔 Уведомления", "callback_data": encode_callback("n", "st", "notif")},
         {"text": "📊 Данные", "callback_data": encode_callback("n", "st", "data")}],
        [{"text": "🧭 Как планируем", "callback_data": encode_callback("n", "st", "plan")}],
    ]
    # пункта нет, пока модель вкуса не приехала: кнопка, которая отвечает
    # «пока не умею», хуже отсутствующей кнопки (то же решение, что и в setMyCommands)
    if taste_ready:
        rows.append([{"text": "😋 Вкусы",
                      "callback_data": encode_callback("n", "ts", "cards")}])
    text = MENU_TEXT.format(
        household=session.get("household_name") or "—",
        role=ROLE_LABELS.get(str(session.get("role")), session.get("role")),
    )
    if not _can_edit(session):
        text += f"\n\n{NO_RIGHTS_TEXT}"
    return Reply(text, build_keyboard(rows))


async def begin(dialogs: Any, app_repository: Any, session: dict[str, Any],
                user_id: int) -> Reply:
    await dialogs.save(user_id, DialogState(SCENE, "menu", {}))
    return menu_reply(session, taste_scene.available(app_repository))


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


async def notifications_reply(bot_repository: Any, telegram_id: int) -> Reply:
    """Тумблеры напоминаний (§6). Строки в базе появляются при первом нажатии,
    до этого действуют умолчания из кода."""
    stored = await bot_repository.notification_settings(telegram_id)
    rows = []
    lines = ["🔔 Напоминания — пишу первым, когда есть повод."]
    for code, kind in notifications.KINDS.items():
        enabled, hour, _last = notifications.setting_for(code, stored)
        lines.append(f"• {kind.title} — {'в ' + str(hour) + ':00' if enabled else 'выключено'}")
        rows.append([{
            "text": button_text(f"{'✅' if enabled else '☐'} {kind.title}"),
            "callback_data": encode_callback("o", "nt", code),
        }])
    rows.append(_back_row())
    return Reply("\n".join(lines), build_keyboard(rows))


async def toggle_notification(bot_repository: Any, telegram_id: int,
                              code: str) -> CallbackReply:
    if code not in notifications.KINDS:
        return CallbackReply(toast="Не понял кнопку.")
    stored = await bot_repository.notification_settings(telegram_id)
    enabled, hour, _last = notifications.setting_for(code, stored)
    await bot_repository.set_notification(telegram_id, code, not enabled, hour)
    return CallbackReply(edit=await notifications_reply(bot_repository, telegram_id))


# --- как планируем (TZ-M8 §3.4) ------------------------------------------------

def _budget_line(profile: dict[str, Any]) -> str:
    budget = profile.get("weekly_budget_kop")
    return f"{int(budget) // 100} ₽ в неделю" if budget else "без ограничения"


def _minutes_line(value: Any) -> str:
    return f"{int(value)} мин" if value else "без лимита"


def _time_summary(profile: dict[str, Any]) -> str:
    return ", ".join(
        f"{title.lower()} {_minutes_line(profile.get(field))}"
        for field, (title, _step) in TIME_LIMITS.items()
    )


def _plan_profile_text(profile: dict[str, Any]) -> str:
    meals = [MEAL_NAMES[code] for code in profile.get("meals") or [] if code in MEAL_NAMES]
    return "\n".join([
        "🧭 Как планируем — это подставляется в мастер меню.",
        f"• Режим: {PLAN_MODES.get(str(profile.get('mode')), profile.get('mode'))}",
        f"• Дней по умолчанию: {profile.get('default_days')}",
        f"• Планируем: {', '.join(meals) if meals else 'ничего'}",
        f"• На два раза: {'да' if profile.get('allow_leftovers') else 'нет'}",
        f"• Нового в меню: {NOVELTY_LEVELS.get(str(profile.get('novelty')), '—')}",
        f"• Кухни: {CUISINE_MODES.get(str(profile.get('cuisine_mode')), '—')}",
        f"• Бюджет: {_budget_line(profile)}",
        f"• Время готовки: {_time_summary(profile)}",
        f"• Повторов блюда за план: не больше "
        f"{profile.get('max_repeats_per_horizon')}",
    ])


async def plan_profile_reply(app_repository: Any, session: dict[str, Any]) -> Reply:
    """Профиль планирования: что видно, то и меняется одной кнопкой."""
    profile = await app_repository.plan_profile(session)
    meals = set(profile.get("meals") or [])
    rows = [
        [{"text": "🎛 Режим", "callback_data": encode_callback("n", "st", "pmode")},
         {"text": "🗓 Горизонт", "callback_data": encode_callback("n", "st", "pdays")}],
        [{
            "text": button_text(f"{'✅' if code in meals else '☐'} {name}"),
            "callback_data": encode_callback("o", "pm", code),
        } for code, name in MEAL_NAMES.items()],
        [{
            "text": button_text(
                f"{'✅' if profile.get('allow_leftovers') else '☐'} На два раза"
            ),
            "callback_data": encode_callback("o", "pl", "left"),
        }],
        [{"text": "🆕 Сколько нового", "callback_data": encode_callback("n", "st", "pnov")},
         {"text": f"🌍 Кухни: {CUISINE_MODES[str(profile.get('cuisine_mode', 'only'))]}",
          "callback_data": encode_callback("o", "pl", "cmode")}],
        [{"text": "💰 Бюджет на неделю", "callback_data": encode_callback("n", "st", "pbud")}],
        [{"text": "⏱ Время готовки", "callback_data": encode_callback("n", "st", "ptime")},
         {"text": "🔁 Повторы", "callback_data": encode_callback("n", "st", "prep")}],
        _back_row(),
    ]
    return Reply(_plan_profile_text(profile), build_keyboard(rows))


def _choice_reply(title: str, action: str, options: dict[str, str], current: Any) -> Reply:
    rows = [[{
        "text": button_text(f"{'✅' if code == str(current) else '☐'} {label}"),
        "callback_data": encode_callback("n", "st", action, code),
    }] for code, label in options.items()]
    rows.append([{"text": "◀ Назад", "callback_data": encode_callback("n", "st", "plan")}])
    return Reply(title, build_keyboard(rows))


async def save_plan_field(app_repository: Any, session: dict[str, Any],
                          field: str, value: Any) -> Reply:
    """Профиль сохраняется целиком: репозиторий делает UPSERT всех колонок и
    неуказанное сбросил бы к умолчаниям."""
    profile = await app_repository.plan_profile(session)
    await app_repository.save_plan_profile(session, {**profile, field: value})
    return await plan_profile_reply(app_repository, session)


def time_limits_reply(profile: dict[str, Any]) -> Reply:
    """Сколько времени на готовку есть в будни, в выходные и на завтрак.

    Лимит — не запрет: рецепт без указанного времени всё равно попадёт в план
    (TZ-M8 §3.5), а слишком долгий получит штраф, а не отсев.
    """
    lines = ["⏱ Сколько времени на готовку:"]
    rows = []
    for field, (title, _step) in TIME_LIMITS.items():
        lines.append(f"• {title}: {_minutes_line(profile.get(field))}")
        rows.append([{
            "text": button_text(f"{title}: {_minutes_line(profile.get(field))}"),
            "callback_data": encode_callback("n", "st", "ptime", field),
        }])
    rows.append([{"text": "◀ Назад", "callback_data": encode_callback("n", "st", "plan")}])
    return Reply("\n".join(lines), build_keyboard(rows))


def ask_minutes(title: str, current: Any) -> Reply:
    return Reply(
        f"Сколько минут на готовку — {title.lower()}?\n"
        f"Сейчас: {_minutes_line(current)}. «Нет» — снять лимит.",
        build_keyboard([[CANCEL_BUTTON]]),
    )


def repeats_reply(profile: dict[str, Any]) -> Reply:
    current = profile.get("max_repeats_per_horizon")
    rows = [[{
        "text": button_text(f"{'✅' if count == current else '☐'} {count}"),
        "callback_data": encode_callback("n", "st", "prep", count),
    } for count in REPEAT_CHOICES]]
    rows.append([{"text": "◀ Назад", "callback_data": encode_callback("n", "st", "plan")}])
    return Reply(
        "Сколько раз одно блюдо может повториться за план?\n"
        "Единица — все блюда разные; больше — меню дешевле, но однообразнее.",
        build_keyboard(rows),
    )


async def toggle_plan_profile(app_repository: Any, session: dict[str, Any],
                             scope: str, code: str) -> CallbackReply:
    """Тумблеры профиля: приёмы пищи («pm») и флаги («pl»)."""
    if not _can_edit(session):
        return CallbackReply(toast=NO_RIGHTS_TEXT, show_alert=True)
    profile = await app_repository.plan_profile(session)
    if scope == "pm":
        if code not in MEAL_NAMES:
            return CallbackReply(toast="Не понял кнопку.")
        meals = [item for item in (profile.get("meals") or []) if item in MEAL_NAMES]
        meals = [item for item in meals if item != code] if code in meals else [*meals, code]
        if not meals:
            # План без единого приёма собрать нельзя, и молча выключать всё —
            # худший способ об этом сообщить.
            return CallbackReply(
                toast="Хотя бы один приём пищи нужен.", show_alert=True
            )
        # порядок дня, а не порядок нажатий
        value = [item for item in MEAL_NAMES if item in meals]
        return CallbackReply(edit=await save_plan_field(
            app_repository, session, "meals", value))
    if code == "left":
        return CallbackReply(edit=await save_plan_field(
            app_repository, session, "allow_leftovers",
            not profile.get("allow_leftovers")))
    if code == "cmode":
        current = str(profile.get("cuisine_mode", "only"))
        return CallbackReply(edit=await save_plan_field(
            app_repository, session, "cuisine_mode",
            "prefer" if current == "only" else "only"))
    return CallbackReply(toast="Не понял кнопку.")


def ask_weekly_budget(profile: dict[str, Any]) -> Reply:
    return Reply(
        "Бюджет на неделю в рублях? Он растягивается на горизонт плана.\n"
        f"Сейчас: {_budget_line(profile)}. «Нет» — снять ограничение.",
        build_keyboard([[CANCEL_BUTTON]]),
    )


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

    if step == "plan_budget":
        value = text.lower().replace(" ", "").replace("₽", "").replace("руб", "")
        if value in {"нет", "-", "без", "безбюджета", "любой", "0"}:
            budget = None
        else:
            digits = "".join(char for char in value if char.isdigit())
            if not digits or not 100 <= int(digits) <= 100_000:
                profile = await ctx.app_repository.plan_profile(session)
                return Reply("Сумма от 100 до 100 000 ₽ или «нет».",
                             ask_weekly_budget(profile).keyboard)
            budget = int(digits) * 100
        await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "menu", {}))
        try:
            return await save_plan_field(
                ctx.app_repository, session, "weekly_budget_kop", budget
            )
        except PermissionError as exc:
            return Reply(str(exc), build_keyboard([_back_row()]))

    if step in {field_step for _title, field_step in TIME_LIMITS.values()}:
        field = next(
            name for name, (_title, field_step) in TIME_LIMITS.items()
            if field_step == step
        )
        title = TIME_LIMITS[field][0]
        value = text.lower().replace(" ", "")
        if value in {"нет", "-", "без", "безлимита", "любое"}:
            minutes = None
        else:
            digits = "".join(char for char in value if char.isdigit())
            if not digits or not 5 <= int(digits) <= 600:
                profile = await ctx.app_repository.plan_profile(session)
                return Reply("Минуты: число от 5 до 600 или «нет».",
                             ask_minutes(title, profile.get(field)).keyboard)
            minutes = int(digits)
        await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "menu", {}))
        try:
            await save_plan_field(ctx.app_repository, session, field, minutes)
        except PermissionError as exc:
            return Reply(str(exc), build_keyboard([_back_row()]))
        return time_limits_reply(await ctx.app_repository.plan_profile(session))

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
        return CallbackReply(
            edit=menu_reply(session, taste_scene.available(app_repository))
        )
    if action == "family":
        return CallbackReply(edit=await family_reply(app_repository, session))
    if action == "appl":
        return CallbackReply(edit=await appliances_reply(app_repository, session))
    if action == "rules":
        return CallbackReply(edit=await rules_reply(app_repository, session))
    if action == "data":
        return CallbackReply(edit=await data_reply(app_repository, session))
    if action == "plan":
        return CallbackReply(edit=await plan_profile_reply(app_repository, session))
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

    if action == "ptime":
        profile = await app_repository.plan_profile(session)
        if not value:
            return CallbackReply(edit=time_limits_reply(profile))
        if value not in TIME_LIMITS:
            return CallbackReply(toast="Не понял кнопку.")
        title, step = TIME_LIMITS[value]
        await dialogs.save(user_id, DialogState(SCENE, step, {}))
        return CallbackReply(edit=ask_minutes(title, profile.get(value)))
    if action == "prep":
        profile = await app_repository.plan_profile(session)
        if not value:
            return CallbackReply(edit=repeats_reply(profile))
        if not str(value).isdigit() or int(value) not in REPEAT_CHOICES:
            return CallbackReply(toast="Не понял кнопку.")
        return CallbackReply(edit=await save_plan_field(
            app_repository, session, "max_repeats_per_horizon", int(value)))

    if action in {"pmode", "pdays", "pnov", "pbud"}:
        profile = await app_repository.plan_profile(session)
        if action == "pmode" and not value:
            return CallbackReply(edit=_choice_reply(
                "Как планировать?", "pmode", PLAN_MODES, profile.get("mode")))
        if action == "pnov" and not value:
            return CallbackReply(edit=_choice_reply(
                "Сколько непробованного в меню?", "pnov", NOVELTY_LEVELS,
                profile.get("novelty")))
        if action == "pdays" and not value:
            rows = [[{
                "text": button_text(
                    f"{'✅' if days == profile.get('default_days') else '☐'} {days} дн."
                ),
                "callback_data": encode_callback("n", "st", "pdays", days),
            } for days in DAY_CHOICES]]
            rows.append([{"text": "◀ Назад",
                          "callback_data": encode_callback("n", "st", "plan")}])
            return CallbackReply(edit=Reply("На сколько дней планировать обычно?",
                                            build_keyboard(rows)))
        if action == "pbud":
            await dialogs.save(user_id, DialogState(SCENE, "plan_budget", {}))
            return CallbackReply(edit=ask_weekly_budget(profile))
        if action == "pmode" and value in PLAN_MODES:
            return CallbackReply(edit=await save_plan_field(
                app_repository, session, "mode", value))
        if action == "pnov" and value in NOVELTY_LEVELS:
            return CallbackReply(edit=await save_plan_field(
                app_repository, session, "novelty", value))
        if action == "pdays" and str(value).isdigit() and int(value) in DAY_CHOICES:
            return CallbackReply(edit=await save_plan_field(
                app_repository, session, "default_days", int(value)))
        return CallbackReply(toast="Не понял кнопку.")

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

