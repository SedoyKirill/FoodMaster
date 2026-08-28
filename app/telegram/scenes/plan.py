"""Меню целиком из чата: мастер сборки, просмотр по дням, история (§5.3–5.5).

Мастер — пять вопросов и сводка. На каждом шаге видно, что уже выбрано, есть
кнопки быстрых значений и «✖ Отмена», а свободный текст принимается всегда:
человек в чате быстрее напечатает «12 сентября», чем найдёт её среди кнопок.

Сборка плана — тяжёлая операция (скоринг и CP-SAT занимают десятки секунд),
поэтому она уходит в фон с плейсхолдером, как замена блюда.
"""

from __future__ import annotations

import re
import uuid as uuid_mod
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.web.payloads import PRICE_TIERS

from ..callbacks import encode_callback, pack_uuid, unpack_uuid
from ..fsm import CANCEL_BUTTON, DialogState
from ..render import (
    CARDS_PER_PAGE, MEAL_LABELS, CallbackReply, Reply, build_keyboard,
    button_text, paginate, pager_row,
)
from . import SceneContext

#: имя сцены в telegram_dialog_state
SCENE = "plan.new"

#: §5.3, шаг 2. До TZ-M8 горизонт ограничен семью днями: и PlanPayload, и
#: CHECK в meal_plans допускают 1–7. Четырнадцать приедут вместе с M8 T3.
DAY_CHOICES = (3, 5, 7)
BUDGET_CHOICES = (3000, 5000)
#: §5.3, шаг 4. Пять режимов планирования — это TZ-M8; пока три ценовые
#: стратегии, ровно те же, что в форме браузера.
TIER_LABELS = {
    "economy": "💰 Экономно",
    "balanced": "⚖️ Сбалансированно",
    "premium": "✨ Премиально",
}
#: сколько кухонь показываем чипами: клавиатура должна оставаться читаемой
CUISINE_LIMIT = 12

STEPS = ("start", "days", "budget", "tier", "cuisines", "confirm")

_WEEKDAY_NAMES = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)
_MONTH_NAMES = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

#: бейджи предупреждений блюда — те же слова, что в браузере (views/plan.js)
MEAL_BADGES = {
    "draft": "черновик",
    "scale_unknown": "порции как в книге",
    "cuisine_fallback": "кухня не совпала",
}


# --- разбор свободного ввода ---------------------------------------------------

def parse_start_date(text: str, today: date) -> date | None:
    """«сегодня», «завтра», «понедельник», «12.09», «12 сентября»."""
    value = (text or "").strip().lower().replace("ё", "е")
    if not value:
        return None
    if value in {"сегодня", "today"}:
        return today
    if value in {"завтра", "tomorrow"}:
        return today + timedelta(days=1)
    if value == "послезавтра":
        return today + timedelta(days=2)
    for index, name in enumerate(_WEEKDAY_NAMES):
        if value == name.replace("ё", "е"):
            ahead = (index - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    numeric = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", value)
    if numeric:
        day, month, year = numeric.groups()
        year = int(year or today.year)
        if year < 100:
            year += 2000
        try:
            return date(year, int(month), int(day))
        except ValueError:
            return None
    worded = re.fullmatch(r"(\d{1,2})\s+([а-я]+)", value)
    if worded:
        day, month_name = worded.groups()
        for index, name in enumerate(_MONTH_NAMES, 1):
            if name.startswith(month_name[:4]):
                try:
                    parsed = date(today.year, index, int(day))
                except ValueError:
                    return None
                # «12 января» в декабре — это следующий год, а не прошедший
                return parsed if parsed >= today else date(today.year + 1, index, int(day))
    return None


def parse_days(text: str) -> int | None:
    match = re.search(r"\d+", text or "")
    if not match:
        return None
    days = int(match.group())
    return days if 1 <= days <= max(DAY_CHOICES) else None


def parse_budget(text: str) -> int | None | str:
    """Рубли → копейки. None — «без бюджета», строка — текст ошибки."""
    value = (text or "").strip().lower().replace(" ", "").replace(" ", "")
    if value in {"без бюджета", "безбюджета", "нет", "-", "любой"}:
        return None
    value = value.replace("₽", "").replace("руб", "").replace(",", ".")
    try:
        rubles = Decimal(value)
    except (InvalidOperation, ValueError):
        return "Не понял сумму. Напишите число, например 5000, или «без бюджета»."
    if rubles <= 0 or rubles > 1_000_000:
        return "Бюджет должен быть от 1 до 1 000 000 ₽."
    return int(rubles * 100)


# --- шаги мастера --------------------------------------------------------------

def _chosen_lines(data: dict[str, Any]) -> list[str]:
    """Что уже выбрано — видно на каждом шаге (§4.2)."""
    lines = []
    if data.get("starts_on"):
        lines.append(f"Старт: {_date_label(date.fromisoformat(data['starts_on']))}")
    if data.get("days"):
        lines.append(f"Дней: {data['days']}")
    if "budget_kop" in data:
        budget = data["budget_kop"]
        lines.append(f"Бюджет: {budget // 100} ₽" if budget else "Бюджет: без ограничения")
    if data.get("price_tier"):
        lines.append(f"Режим: {TIER_LABELS[data['price_tier']]}")
    if "cuisines" in data:
        chosen = data["cuisines"]
        lines.append("Кухни: " + (", ".join(chosen) if chosen else "любые"))
    return lines


def _date_label(value: date) -> str:
    return f"{_WEEKDAY_NAMES[value.weekday()]}, {value.day} {_MONTH_NAMES[value.month - 1]}"


def unpack_plan_id(value: Any) -> Any:
    """id плана приходит и строкой, и UUID — репозиторий ждёт UUID."""
    return value if isinstance(value, uuid_mod.UUID) else uuid_mod.UUID(str(value))


def _step_button(field: str, value: Any, label: str) -> dict[str, Any]:
    return {"text": label, "callback_data": encode_callback("n", "pl", field, value)}


def step_reply(step: str, data: dict[str, Any], cuisines: list[str] | None = None) -> Reply:
    """Вопрос очередного шага вместе с кнопками быстрых значений."""
    prefix = "\n".join(_chosen_lines(data))
    prefix = f"{prefix}\n\n" if prefix else ""

    if step == "start":
        rows = [[
            _step_button("start", "today", "Сегодня"),
            _step_button("start", "tomorrow", "Завтра"),
            _step_button("start", "monday", "С понедельника"),
        ]]
        text = "С какого дня составить меню? Можно написать дату: «12.09»."
    elif step == "days":
        rows = [[_step_button("days", days, f"{days}") for days in DAY_CHOICES]]
        text = "На сколько дней? Можно написать число от 1 до 7."
    elif step == "budget":
        rows = [
            [_step_button("budget", amount, f"{amount} ₽") for amount in BUDGET_CHOICES],
            [_step_button("budget", "none", "Без бюджета")],
        ]
        text = "Какой бюджет на продукты? Можно написать сумму в рублях."
    elif step == "tier":
        rows = [[_step_button("tier", code, label)] for code, label in TIER_LABELS.items()]
        text = "Как выбирать продукты?"
    elif step == "cuisines":
        chosen = set(data.get("cuisines") or [])
        chips = [
            {
                "text": button_text(f"{'✅' if code in chosen else '☐'} {code}"),
                "callback_data": encode_callback("o", code),
            }
            for code in (cuisines or [])[:CUISINE_LIMIT]
        ]
        rows = [chips[index:index + 2] for index in range(0, len(chips), 2)]
        rows.append([_step_button("cuisines", "any", "Любая кухня")])
        rows.append([_step_button("cuisines", "done", "Готово")])
        text = (
            "Какие кухни предпочесть? Отмечайте нужные и жмите «Готово».\n"
            "Кухня — жёсткий фильтр: если блюд не хватит, слот подберётся из общего пула."
        )
    else:  # confirm
        rows = [[_step_button("go", "1", "✅ Составить меню")]]
        text = "Всё верно?"

    rows.append([CANCEL_BUTTON])
    return Reply(f"{prefix}{text}", build_keyboard(rows))


async def begin(dialogs: Any, user_id: int) -> Reply:
    await dialogs.save(user_id, DialogState(SCENE, "start", {}))
    return step_reply("start", {})


def _next_step(step: str) -> str:
    return STEPS[min(STEPS.index(step) + 1, len(STEPS) - 1)]


async def handle_step(ctx: SceneContext) -> Reply:
    """Свободный текст на текущем шаге мастера."""
    state = ctx.state
    data = dict(state.data or {})
    step = state.step or "start"

    if step == "start":
        parsed = parse_start_date(ctx.text, ctx.today)
        if parsed is None:
            return Reply("Не понял дату. Напишите «сегодня», «завтра» или «12.09».",
                         step_reply(step, data).keyboard)
        data["starts_on"] = parsed.isoformat()
    elif step == "days":
        days = parse_days(ctx.text)
        if days is None:
            return Reply(f"Напишите число от 1 до {max(DAY_CHOICES)}.",
                         step_reply(step, data).keyboard)
        data["days"] = days
    elif step == "budget":
        budget = parse_budget(ctx.text)
        if isinstance(budget, str):
            return Reply(budget, step_reply(step, data).keyboard)
        data["budget_kop"] = budget
    elif step == "tier":
        return Reply("Выберите режим кнопкой.", step_reply(step, data).keyboard)
    elif step == "cuisines":
        return Reply("Отмечайте кухни кнопками и жмите «Готово».",
                     step_reply(step, data, await _cuisines(ctx.app_repository)).keyboard)
    else:
        return Reply("Нажмите «✅ Составить меню» или «✖ Отмена».",
                     step_reply("confirm", data).keyboard)

    return await _advance(ctx.dialogs, ctx.app_repository, ctx.actor.user_id, step, data)


async def _advance(dialogs: Any, app_repository: Any, user_id: int, step: str,
                   data: dict[str, Any]) -> Reply:
    next_step = _next_step(step)
    await dialogs.save(user_id, DialogState(SCENE, next_step, data))
    cuisines = await _cuisines(app_repository) if next_step == "cuisines" else None
    if next_step == "cuisines" and not cuisines:
        # Библиотека без разметки кухонь — вопрос без вариантов задавать незачем
        data["cuisines"] = []
        await dialogs.save(user_id, DialogState(SCENE, "confirm", data))
        return step_reply("confirm", data)
    return step_reply(next_step, data, cuisines)


async def _cuisines(app_repository: Any) -> list[str]:
    facets = await app_repository.recipe_facets()
    return [str(item) for item in (facets or {}).get("cuisines") or []]


async def handle_callback(
    app_repository: Any, dialogs: Any, session: dict[str, Any], user_id: int,
    parts: list[str], today: date,
) -> CallbackReply | None:
    """Кнопки мастера: быстрые значения, чипы кухонь, запуск сборки.

    None — кнопка не наша, пусть разбирается общий обработчик.
    """
    state = await dialogs.load(user_id) if dialogs is not None else None
    field = parts[1] if len(parts) > 1 else ""

    if field == "new":  # «➕ Составить меню» — вход в мастер
        return CallbackReply(edit=await begin(dialogs, user_id))
    if state is None or state.scene != SCENE:
        return CallbackReply(toast="Мастер меню закрыт — начните заново.", show_alert=True)

    data = dict(state.data or {})
    value = parts[2] if len(parts) > 2 else ""

    if field == "start":
        offsets = {"today": 0, "tomorrow": 1}
        if value in offsets:
            data["starts_on"] = (today + timedelta(days=offsets[value])).isoformat()
        else:
            ahead = (0 - today.weekday()) % 7 or 7
            data["starts_on"] = (today + timedelta(days=ahead)).isoformat()
    elif field == "days":
        data["days"] = int(value)
    elif field == "budget":
        data["budget_kop"] = None if value == "none" else int(value) * 100
    elif field == "tier":
        if value not in PRICE_TIERS:
            return CallbackReply(toast="Не понял кнопку.")
        data["price_tier"] = value
    elif field == "cuisines":
        data["cuisines"] = [] if value == "any" else list(data.get("cuisines") or [])
        await dialogs.save(user_id, DialogState(SCENE, "confirm", data))
        return CallbackReply(edit=step_reply("confirm", data))
    elif field == "go":
        return await build(app_repository, dialogs, session, user_id, today)
    else:
        return None

    return CallbackReply(
        edit=await _advance(dialogs, app_repository, user_id, field_to_step(field), data)
    )


def field_to_step(field: str) -> str:
    """Поле кнопки → шаг мастера (кнопка «days» завершает шаг «days»)."""
    return field if field in STEPS else "start"


async def toggle_cuisine(dialogs: Any, app_repository: Any, user_id: int,
                         code: str) -> CallbackReply:
    """Чип кухни в мастере (глагол ``o``)."""
    state = await dialogs.load(user_id) if dialogs is not None else None
    if state is None or state.scene != SCENE or state.step != "cuisines":
        return CallbackReply(toast="Кнопка устарела — откройте мастер заново.", show_alert=True)
    data = dict(state.data or {})
    chosen = list(data.get("cuisines") or [])
    data["cuisines"] = [item for item in chosen if item != code] if code in chosen else [*chosen, code]
    await dialogs.save(user_id, DialogState(SCENE, "cuisines", data))
    return CallbackReply(
        edit=step_reply("cuisines", data, await _cuisines(app_repository))
    )


async def build(app_repository: Any, dialogs: Any, session: dict[str, Any],
                user_id: int, today: date) -> CallbackReply:
    """Финальный шаг: собрать план и показать первый день."""
    state = await dialogs.load(user_id) if dialogs is not None else None
    if state is None or state.scene != SCENE:
        return CallbackReply(toast="Мастер меню закрыт — начните заново.", show_alert=True)
    data = dict(state.data or {})
    starts_on = date.fromisoformat(data.get("starts_on") or today.isoformat())
    try:
        plan = await app_repository.create_plan(
            session,
            starts_on=starts_on,
            days=int(data.get("days") or DAY_CHOICES[0]),
            budget_kop=data.get("budget_kop"),
            cuisines=list(data.get("cuisines") or []),
            price_tier=data.get("price_tier") or "balanced",
        )
    except PermissionError as exc:
        await dialogs.clear(user_id)
        return CallbackReply(edit=Reply(str(exc)))
    except ValueError as exc:
        # §5.3: не из чего собирать — говорим прямо и даём вернуться к кухням
        data.pop("cuisines", None)
        await dialogs.save(user_id, DialogState(SCENE, "cuisines", data))
        keyboard = step_reply("cuisines", data, await _cuisines(app_repository)).keyboard
        return CallbackReply(edit=Reply(f"{exc}\n\nПопробуем другие кухни?", keyboard))
    await dialogs.clear(user_id)
    # Перечитываем из базы: у блюд, вернувшихся из решателя, ещё нет id, а без
    # них кнопки «рецепт» и «заменить» некуда адресовать.
    saved = await app_repository.get_plan(session, unpack_plan_id(plan["id"]))
    return CallbackReply(edit=day_reply(saved or plan, 1))


# --- просмотр плана ------------------------------------------------------------

def plan_header(plan: dict[str, Any]) -> str:
    """Шапка активного плана: деньги, покрытие, предупреждения (§5.4)."""
    starts_on = plan.get("starts_on")
    if isinstance(starts_on, str):
        starts_on = date.fromisoformat(starts_on)
    days = int(plan.get("days") or 0)
    # без дня недели: «Меню с суббота» — не по-русски, а склонять ради шапки
    # незачем, день недели и так виден в строке самого дня
    lines = [
        f"📅 Меню с {starts_on.day} {_MONTH_NAMES[starts_on.month - 1]} · {days} дн."
    ]

    cost = plan.get("estimated_cost_kop")
    budget = plan.get("budget_kop")
    if cost is not None:
        money = f"Стоимость ≈{int(cost) // 100} ₽"
        if budget:
            money += f" из {int(budget) // 100} ₽"
            if int(cost) > int(budget):
                money += " — бюджет превышен"
        lines.append(money)

    matched = plan.get("matched_cost_items")
    total = plan.get("total_cost_items")
    if matched is not None and total:
        share = round(100 * int(matched) / int(total))
        # двоеточие вместо «позиций»: числительное с существительным пришлось
        # бы склонять, а пользы от слова здесь нет
        lines.append(f"Сопоставлено с каталогом: {matched} из {total} ({share} %)")

    tier = TIER_LABELS.get(str(plan.get("price_tier")))
    if tier:
        lines.append(f"Режим: {tier}")
    warnings = plan.get("warnings") or []
    if warnings:
        lines.append(f"Предупреждений: {len(warnings)}")
    return "\n".join(lines)


def plan_days(plan: dict[str, Any]) -> list[date]:
    seen: list[date] = []
    for meal in plan.get("meals") or []:
        meal_date = meal.get("meal_date")
        if isinstance(meal_date, str):
            meal_date = date.fromisoformat(meal_date)
        if meal_date and meal_date not in seen:
            seen.append(meal_date)
    return seen


def _meal_badges(meal: dict[str, Any]) -> str:
    labels = []
    for code in meal.get("warnings") or []:
        code = str(code)
        if code in MEAL_BADGES:
            labels.append(MEAL_BADGES[code])
        elif code.startswith("kcal_partial:"):
            labels.append(f"ккал по {code.split(':', 1)[1]} ингр.")
    return f"\n  ⚠ {', '.join(labels)}" if labels else ""


def day_reply(plan: dict[str, Any], day_number: int) -> Reply:
    """Один день плана: блюда, бейджи и кнопки на каждое блюдо (§5.4)."""
    days = plan_days(plan)
    if not days:
        return Reply(f"{plan_header(plan)}\n\nВ плане нет блюд.", plan_keyboard(plan, 1, []))
    day_number = min(max(int(day_number or 1), 1), len(days))
    current = days[day_number - 1]
    meals = [
        meal for meal in plan["meals"]
        if (date.fromisoformat(meal["meal_date"]) if isinstance(meal["meal_date"], str)
            else meal["meal_date"]) == current
    ]
    lines = [plan_header(plan), "", f"День {day_number} из {len(days)} — {_date_label(current)}"]
    for meal in meals:
        label = MEAL_LABELS.get(str(meal.get("meal_type")), "Блюдо")
        kcal = meal.get("estimated_kcal")
        kcal_text = f" · ≈{kcal} ккал" if kcal is not None else ""
        lines.append(f"• {label}: {meal.get('title')}{kcal_text}{_meal_badges(meal)}")
    known = [meal["estimated_kcal"] for meal in meals if meal.get("estimated_kcal") is not None]
    if known:
        lines.append(f"Итого ≈{sum(known)} ккал")
    return Reply("\n".join(lines), plan_keyboard(plan, day_number, meals))


def plan_keyboard(plan: dict[str, Any], day_number: int, meals: list[dict[str, Any]]) -> dict | None:
    plan_id = pack_uuid(plan["id"])
    rows = []
    for meal in meals:
        if not meal.get("id"):
            continue
        label = MEAL_LABELS.get(str(meal.get("meal_type")), "Блюдо")
        packed = pack_uuid(meal["id"])
        rows.append([
            {"text": f"📖 {label}", "callback_data": encode_callback("r", plan_id, packed)},
            {"text": f"🔁 {label}", "callback_data": encode_callback("x", plan_id, packed)},
        ])
    total_days = len(plan_days(plan))
    if total_days > 1:
        nav = []
        if day_number > 1:
            nav.append({"text": "◀", "callback_data": encode_callback("d", plan_id, day_number - 1)})
        nav.append({"text": f"{day_number}/{total_days}", "callback_data": encode_callback("n", "noop")})
        if day_number < total_days:
            nav.append({"text": "▶", "callback_data": encode_callback("d", plan_id, day_number + 1)})
        rows.append(nav)
    rows.append([
        {"text": "🛒 Покупки", "callback_data": encode_callback("p", "sh", plan_id, 1)},
        {"text": "🗂 История", "callback_data": encode_callback("p", "pl", 1)},
    ])
    rows.append([
        {"text": "➕ Новое меню", "callback_data": encode_callback("n", "pl", "new")},
        {"text": "🗑 Удалить", "callback_data": encode_callback("y", "pd", plan_id)},
    ])
    return build_keyboard(rows)


# --- история (§5.4) ------------------------------------------------------------

async def history_reply(app_repository: Any, session: dict[str, Any], page: int = 1) -> Reply:
    plans = await app_repository.list_plans(session, 20)
    if not plans:
        return Reply(
            "🗂 Планов пока нет.",
            build_keyboard([[{
                "text": "➕ Составить меню",
                "callback_data": encode_callback("n", "pl", "new"),
            }]]),
        )
    current = paginate(plans, page, CARDS_PER_PAGE)
    rows = []
    for plan in current.items:
        starts_on = plan.get("starts_on")
        if isinstance(starts_on, str):
            starts_on = date.fromisoformat(starts_on)
        cost = plan.get("estimated_cost_kop")
        cost_text = f" · {int(cost) // 100} ₽" if cost else ""
        rows.append([{
            "text": button_text(
                f"{starts_on.day} {_MONTH_NAMES[starts_on.month - 1]}"
                f" · {plan.get('days')} дн.{cost_text}"
            ),
            "callback_data": encode_callback("d", pack_uuid(plan["id"]), 1),
        }])
    rows.append(pager_row("pl", current))
    rows.append([{
        "text": "➕ Составить меню",
        "callback_data": encode_callback("n", "pl", "new"),
    }])
    return Reply(
        f"🗂 История планов — {current.total} шт. Откройте любой, чтобы посмотреть дни.",
        build_keyboard(rows),
    )


async def delete(app_repository: Any, session: dict[str, Any], packed_plan: str) -> CallbackReply:
    plan_id = unpack_uuid(packed_plan)
    if plan_id is None:
        return CallbackReply(toast="Не понял кнопку.")
    try:
        deleted = await app_repository.delete_plan(session, plan_id)
    except PermissionError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    if not deleted:
        return CallbackReply(toast="Этот план уже удалён.", show_alert=True)
    return CallbackReply(edit=Reply("🗑 План удалён."))
