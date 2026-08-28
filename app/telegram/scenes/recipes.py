"""Библиотека рецептов в чате: поиск, фильтры, карточка, оценка (§5.7).

Три тысячи рецептов листать кнопками бессмысленно, поэтому основной способ —
написать название: свободный текст в этой сцене и есть поисковый запрос.
Фильтры (приём пищи, кухня, тип блюда, «только проверенные») живут в состоянии
диалога и показываются в шапке, чтобы пустая выдача не выглядела поломкой.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import date
from typing import Any

from app.web.categories import APPLIANCES, cuisine_label, dish_type_label
from app.web.planner import clean_dish_title

from ..callbacks import encode_callback
from ..fsm import DialogState
from ..render import (
    CARDS_PER_PAGE, MEAL_LABELS, CallbackReply, Reply, build_keyboard,
    button_text, format_recipe,
)
from . import SceneContext

SCENE = "recipes.search"

#: фильтры: код в callback → поле состояния и заголовок выбора
FILTERS = {
    "m": ("meal_type", "Приём пищи"),
    "c": ("cuisine", "Кухня"),
    "d": ("dish_type", "Тип блюда"),
}

REVIEW_LABELS = {
    "ready": "✅ Готов",
    "needs_review": "↩ В черновики",
    "rejected": "❌ Отклонить",
}

SEARCH_HINT = "Напишите название блюда или продукта — найду по библиотеке."

#: сколько вариантов показываем в выборе значения фильтра
FILTER_CHOICES = 12


# --- состояние поиска ----------------------------------------------------------

def _filters(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "search": str(data.get("q") or ""),
        "meal_type": str(data.get("meal_type") or ""),
        "cuisine": str(data.get("cuisine") or ""),
        "dish_type": str(data.get("dish_type") or ""),
        "ready_only": bool(data.get("ready")),
    }


def _filter_summary(data: dict[str, Any]) -> str:
    parts = []
    if data.get("q"):
        parts.append(f"«{data['q']}»")
    if data.get("meal_type"):
        parts.append(MEAL_LABELS.get(data["meal_type"], data["meal_type"]))
    if data.get("cuisine"):
        parts.append(cuisine_label(data["cuisine"]))
    if data.get("dish_type"):
        parts.append(dish_type_label(data["dish_type"]))
    if data.get("ready"):
        parts.append("только проверенные")
    return " · ".join(parts)


def _card_label(recipe: dict[str, Any]) -> str:
    # названия черновиков тянут за собой мусор со страницы книги
    bits = [clean_dish_title(str(recipe.get("title") or "Рецепт"))]
    minutes = recipe.get("time_total_minutes")
    if minutes:
        bits.append(f"{minutes} мин")
    servings = recipe.get("source_servings_min")
    if servings:
        bits.append(f"{servings} порц.")
    if recipe.get("review_status") != "ready":
        bits.append("черновик")
    return " · ".join(bits)


async def results_reply(app_repository: Any, data: dict[str, Any]) -> Reply:
    """Страница выдачи: карточки кнопками, фильтры и пагинация."""
    page = max(int(data.get("page") or 1), 1)
    found = await app_repository.list_recipes(
        limit=CARDS_PER_PAGE, offset=(page - 1) * CARDS_PER_PAGE, **_filters(data)
    )
    items = found.get("items") or []
    total = int(found.get("total") or 0)
    pages = max(1, -(-total // CARDS_PER_PAGE))

    summary = _filter_summary(data)
    if total:
        lines = [f"📖 Нашёл {total} рец. — страница {min(page, pages)} из {pages}."]
    else:
        lines = ["📖 Ничего не нашлось."]
    if summary:
        lines.append(f"Фильтры: {summary}")
    if not total:
        lines.append("Снимите фильтр или напишите другое название.")
    else:
        lines.append(SEARCH_HINT)

    rows = [[{
        "text": button_text(_card_label(recipe)),
        "callback_data": encode_callback("r", int(recipe["id"])),
    }] for recipe in items]
    rows.append(_pager_row(page, pages))
    rows.append([
        {"text": "🍽 Приём", "callback_data": encode_callback("f", "rc", "m")},
        {"text": "🌍 Кухня", "callback_data": encode_callback("f", "rc", "c")},
        {"text": "🍲 Тип", "callback_data": encode_callback("f", "rc", "d")},
    ])
    rows.append([
        {
            "text": ("✅ Только проверенные" if data.get("ready")
                     else "☐ Только проверенные"),
            "callback_data": encode_callback("f", "rc", "r"),
        },
        {"text": "🧹 Сбросить", "callback_data": encode_callback("f", "rc", "x")},
    ])
    return Reply("\n".join(lines), build_keyboard(rows))


def _pager_row(page: int, pages: int) -> list[dict]:
    if pages <= 1:
        return []
    row = []
    if page > 1:
        row.append({"text": "◀", "callback_data": encode_callback("p", "rc", page - 1)})
    row.append({"text": f"{min(page, pages)}/{pages}",
                "callback_data": encode_callback("n", "noop")})
    if page < pages:
        row.append({"text": "▶", "callback_data": encode_callback("p", "rc", page + 1)})
    return row


async def begin(dialogs: Any, app_repository: Any, user_id: int) -> Reply:
    await dialogs.save(user_id, DialogState(SCENE, "query", {}))
    return await results_reply(app_repository, {})


async def handle_step(ctx: SceneContext) -> Reply:
    """Свободный текст в этой сцене — поисковый запрос."""
    data = dict(ctx.state.data or {})
    data["q"] = (ctx.text or "").strip()
    data["page"] = 1
    await ctx.dialogs.save(ctx.actor.user_id, DialogState(SCENE, "query", data))
    return await results_reply(ctx.app_repository, data)


async def _state_data(dialogs: Any, user_id: int) -> dict[str, Any]:
    state = await dialogs.load(user_id) if dialogs is not None else None
    return dict(state.data or {}) if state is not None and state.scene == SCENE else {}


async def _save(dialogs: Any, user_id: int, data: dict[str, Any]) -> None:
    if dialogs is not None:
        await dialogs.save(user_id, DialogState(SCENE, "query", data))


# --- фильтры -------------------------------------------------------------------

async def handle_filter(app_repository: Any, dialogs: Any, user_id: int,
                        parts: list[str]) -> CallbackReply | None:
    """Глагол ``f`` для библиотеки: показать выбор, применить, сбросить."""
    if parts[:1] != ["rc"] or len(parts) < 2:
        return None
    code = parts[1]
    data = await _state_data(dialogs, user_id)

    if code == "r":  # тумблер «только проверенные»
        data["ready"] = not data.get("ready")
        data["page"] = 1
    elif code == "x":  # сброс
        data = {}
    elif code in FILTERS:
        field, title = FILTERS[code]
        if len(parts) > 2:
            data[field] = "" if parts[2] == "-" else parts[2]
            data["page"] = 1
        else:
            return CallbackReply(edit=await _choices_reply(app_repository, code, title, data))
    else:
        return None

    await _save(dialogs, user_id, data)
    return CallbackReply(edit=await results_reply(app_repository, data))


async def _choices_reply(app_repository: Any, code: str, title: str,
                         data: dict[str, Any]) -> Reply:
    field, _ = FILTERS[code]
    if code == "m":
        options = list(MEAL_LABELS.items())
    else:
        facets = await app_repository.recipe_facets() or {}
        # показываем только то, что реально есть в библиотеке: фильтр, который
        # гарантированно даёт ноль, хуже отсутствующего
        raw = facets.get("cuisines" if code == "c" else "dish_types") or []
        to_label = cuisine_label if code == "c" else dish_type_label
        options = [(str(entry), to_label(entry)) for entry in raw][:FILTER_CHOICES]
    current = data.get(field) or ""
    rows = [[{
        "text": f"{'✅' if value == current else '☐'} {label}",
        "callback_data": encode_callback("f", "rc", code, value),
    }] for value, label in options]
    rows.append([{
        "text": "Не важно",
        "callback_data": encode_callback("f", "rc", code, "-"),
    }])
    return Reply(f"{title}: выберите значение.", build_keyboard(rows))


async def handle_page(app_repository: Any, dialogs: Any, user_id: int,
                      parts: list[str]) -> CallbackReply | None:
    if parts[:1] != ["rc"]:
        return None
    data = await _state_data(dialogs, user_id)
    data["page"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    await _save(dialogs, user_id, data)
    return CallbackReply(edit=await results_reply(app_repository, data))


# --- карточка ------------------------------------------------------------------

def _prices_block(detail: dict[str, Any]) -> str:
    lines = ["", "💰 Цены «Ленты»:"]
    total = 0
    for ingredient in detail.get("ingredients") or []:
        product = ingredient.get("matched_product")
        if not product:
            continue
        price = product.get("effective_price_kop")
        name = product.get("name") or ingredient.get("normalized_name")
        if price:
            total += int(price)
            lines.append(f"• {name} — {int(price) // 100} ₽")
        else:
            lines.append(f"• {name}")
    if len(lines) == 2:
        return "\n\n💰 Ни один ингредиент не сопоставлен с каталогом."
    if total:
        lines.append(f"Итого по упаковкам ≈{total // 100} ₽")
    return "\n".join(lines)


def card_reply(detail: dict[str, Any], session: dict[str, Any],
               with_prices: bool = False) -> Reply:
    recipe_id = int(detail["id"])
    text = format_recipe(detail)
    if with_prices:
        text += _prices_block(detail)

    rating = detail.get("my_rating")
    stars = [{
        "text": ("★" if rating and value <= int(rating) else "☆"),
        "callback_data": encode_callback("g", recipe_id, value),
    } for value in range(1, 6)]
    rows = [stars]
    if rating:
        rows.append([{
            "text": "Снять оценку",
            "callback_data": encode_callback("g", recipe_id, 0),
        }])
    rows.append([
        {
            "text": "💰 Цены Ленты" if not with_prices else "📖 Без цен",
            "callback_data": (encode_callback("r", recipe_id, "p") if not with_prices
                              else encode_callback("r", recipe_id)),
        },
        {"text": "➕ В план", "callback_data": encode_callback("n", "rc", "add", recipe_id)},
    ])
    if session.get("role") in {"owner", "admin"}:
        status = detail.get("review_status")
        rows.append([
            {"text": label, "callback_data": encode_callback("w", recipe_id, code)}
            for code, label in REVIEW_LABELS.items() if code != status
        ])
    rows.append([{"text": "◀ К поиску", "callback_data": encode_callback("n", "rc", "back")}])
    return Reply(text, build_keyboard(rows))


async def open_card(app_repository: Any, session: dict[str, Any], recipe_id: int,
                    with_prices: bool = False) -> CallbackReply:
    detail = await app_repository.recipe_detail(recipe_id, session["household_id"])
    if detail is None:
        return CallbackReply(toast="Рецепт недоступен.", show_alert=True)
    return CallbackReply(edit=card_reply(detail, session, with_prices))


async def rate(app_repository: Any, session: dict[str, Any], recipe_id: int,
               value: int) -> CallbackReply:
    try:
        await app_repository.set_recipe_rating(session, recipe_id, value or None)
    except PermissionError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    except ValueError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    result = await open_card(app_repository, session, recipe_id)
    result.toast = "Оценка снята" if not value else f"Оценка: {value}"
    return result


async def review(app_repository: Any, session: dict[str, Any], recipe_id: int,
                 status: str) -> CallbackReply:
    try:
        updated = await app_repository.set_review_status(session, recipe_id, status)
    except (PermissionError, ValueError) as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    if updated is None:
        return CallbackReply(toast="Рецепт недоступен.", show_alert=True)
    result = await open_card(app_repository, session, recipe_id)
    result.toast = REVIEW_LABELS.get(status, status)
    return result


# --- «➕ В план» ----------------------------------------------------------------

def _plan_days(plan: dict[str, Any]) -> list[date]:
    seen: list[date] = []
    for meal in plan.get("meals") or []:
        value = meal.get("meal_date")
        if isinstance(value, str):
            value = date.fromisoformat(value)
        if value and value not in seen:
            seen.append(value)
    return seen


async def choose_day(app_repository: Any, session: dict[str, Any],
                     recipe_id: int) -> CallbackReply:
    plan = await app_repository.latest_plan(session)
    if plan is None:
        return CallbackReply(
            toast="Сначала составьте меню — рецепт некуда ставить.", show_alert=True
        )
    days = _plan_days(plan)
    rows = [[{
        "text": f"День {index} — {value.day:02d}.{value.month:02d}",
        "callback_data": encode_callback("n", "rc", "day", recipe_id, index),
    }] for index, value in enumerate(days, 1)]
    rows.append([{
        "text": "◀ К рецепту",
        "callback_data": encode_callback("r", recipe_id),
    }])
    return CallbackReply(edit=Reply("В какой день поставить блюдо?", build_keyboard(rows)))


async def choose_meal(app_repository: Any, session: dict[str, Any], recipe_id: int,
                      day_number: int) -> CallbackReply:
    plan = await app_repository.latest_plan(session)
    if plan is None:
        return CallbackReply(toast="План не найден.", show_alert=True)
    days = _plan_days(plan)
    if not 1 <= day_number <= len(days):
        return CallbackReply(toast="Такого дня в плане нет.", show_alert=True)
    rows = [[{
        "text": label,
        "callback_data": encode_callback("n", "rc", "set", recipe_id, day_number, code),
    }] for code, label in MEAL_LABELS.items()]
    rows.append([{
        "text": "◀ Назад",
        "callback_data": encode_callback("n", "rc", "add", recipe_id),
    }])
    return CallbackReply(edit=Reply(
        f"Какой приём пищи заменить {days[day_number - 1].day:02d}."
        f"{days[day_number - 1].month:02d}?",
        build_keyboard(rows),
    ))


async def put_in_plan(app_repository: Any, session: dict[str, Any], recipe_id: int,
                      day_number: int, meal_type: str) -> CallbackReply:
    plan = await app_repository.latest_plan(session)
    if plan is None:
        return CallbackReply(toast="План не найден.", show_alert=True)
    days = _plan_days(plan)
    if not 1 <= day_number <= len(days):
        return CallbackReply(toast="Такого дня в плане нет.", show_alert=True)
    try:
        updated = await app_repository.add_to_plan(
            session, _as_uuid(plan["id"]), days[day_number - 1], meal_type, recipe_id
        )
    except PermissionError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)
    except ValueError as exc:
        # рецепт не подходит слоту — та же проверка, что у замены блюда.
        # Голое «нельзя» выглядит поломкой, поэтому объясняем, чем не подошёл.
        hint = await _rejection_hint(app_repository, session, plan, recipe_id)
        return CallbackReply(edit=Reply(f"{exc}.{hint}"), toast=str(exc), show_alert=True)
    if updated is None:
        return CallbackReply(toast="Этот слот не найден в плане.", show_alert=True)
    from . import plan as plan_scene

    return CallbackReply(
        toast="Готово, блюдо в плане",
        edit=plan_scene.day_reply(updated, day_number),
    )


async def _rejection_hint(app_repository: Any, session: dict[str, Any],
                          plan: dict[str, Any], recipe_id: int) -> str:
    """Почему рецепт не встал в слот — только то, что видно из данных.

    Раньше здесь предполагалась кухня плана, но она лишь меняет порядок выдачи
    и никого не отсекает. Настоящих причин две: не хватает техники или блюдо
    уже стоит в плане рядом (повторы ограничены). Если ни то ни другое —
    честно говорим, что дело в ограничениях семьи, и не выдумываем причину.
    """
    detail = await app_repository.recipe_detail(recipe_id, session["household_id"])
    if detail is None:
        return ""
    for meal in plan.get("meals") or []:
        if int(meal.get("recipe_id") or 0) == int(recipe_id):
            return " Это блюдо уже стоит в плане, а повторы подряд ограничены."
    profile = await app_repository.get_profile(session)
    needed = {str(code) for code in (detail.get("appliances") or [])}
    missing = needed - {str(code) for code in (profile.get("appliances") or [])}
    if missing:
        names = ", ".join(sorted(appliance_label(code) for code in missing))
        return f" Для него нужна техника, которой нет в настройках: {names}."
    return " Похоже, мешает строгое ограничение в питании — проверьте настройки."


def _as_uuid(value: Any) -> Any:
    """id плана приходит и строкой, и UUID — репозиторий ждёт UUID."""
    return value if isinstance(value, uuid_mod.UUID) else uuid_mod.UUID(str(value))


async def handle_navigation(app_repository: Any, dialogs: Any, session: dict[str, Any],
                            user_id: int, parts: list[str]) -> CallbackReply | None:
    """Глагол ``n`` для библиотеки: назад к поиску и постановка в план."""
    if parts[:1] != ["rc"] or len(parts) < 2:
        return None
    action = parts[1]
    if action == "back":
        data = await _state_data(dialogs, user_id)
        return CallbackReply(edit=await results_reply(app_repository, data))
    if action == "add" and len(parts) > 2:
        return await choose_day(app_repository, session, int(parts[2]))
    if action == "day" and len(parts) > 3:
        return await choose_meal(app_repository, session, int(parts[2]), int(parts[3]))
    if action == "set" and len(parts) > 4:
        return await put_in_plan(
            app_repository, session, int(parts[2]), int(parts[3]), parts[4]
        )
    return None


def appliance_label(code: str) -> str:
    return APPLIANCES.get(str(code), str(code))
