"""Легаси-обработчики бота и фасад совместимости (TZ-M7 §2).

Модуль разъехался: кодек — в ``callbacks.py``, тексты и клавиатуры — в
``render.py``, запросы — в ``repository.py``, разбор входящего — в
``router.py``. Здесь остались плоский каскад ``handle_message`` /
``handle_callback`` (в T4–T9 его заменят сцены) и реэкспорты, чтобы импорты
не пришлось править одним заходом. После T9 модуль удаляется.

Никакого сетевого кода: на вход — репозитории (или стабы в тестах), на выход —
декларативные Reply/CallbackReply для транспорта.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.web.planner import clean_dish_title

from .callbacks import (
    callback_verb, encode_callback, pack_uuid, parse_callback, unpack_uuid,
)
from .render import (
    BUTTON_TEXT_LIMIT, CallbackReply, HELP_TEXT, MEAL_LABELS, NOT_LINKED_TEXT,
    Reply, STALE_TEXT, TELEGRAM_LIMIT, alternatives_keyboard, format_day,
    format_recipe, format_shopping, format_shopping_header, format_week,
    shopping_keyboard, shopping_page, split_for_telegram, today_keyboard,
)
from .repository import BotRepository, bot_session

__all__ = [
    "BUTTON_TEXT_LIMIT", "BotRepository", "CallbackReply", "HELP_TEXT",
    "MEAL_LABELS", "NOT_LINKED_TEXT", "Reply", "STALE_TEXT", "TELEGRAM_LIMIT",
    "alternatives_keyboard", "bot_session", "callback_verb", "encode_callback",
    "format_day", "format_recipe", "format_shopping", "format_shopping_header",
    "format_week", "handle_callback", "handle_message", "pack_uuid",
    "parse_callback", "shopping_keyboard", "split_for_telegram",
    "today_keyboard", "unpack_uuid",
]


# --- обработка входящих сообщений --------------------------------------------

async def handle_message(
    repository: BotRepository, user_id: int, text: str, today: date
) -> Reply:
    """Ответ на текстовое сообщение. Не бросает — ошибки ловит транспорт.

    ``user_id`` — Telegram ``from.id``: личность, а не чат (TZ-M7 §3.1).
    """
    text = (text or "").strip()
    lowered = text.lower()

    if lowered.startswith("/start"):
        payload = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if payload.startswith("link_"):
            login = await repository.link_user(user_id, payload[len("link_"):])
            if login is None:
                return Reply(
                    "Ссылка привязки не подошла: токен просрочен или уже использован.\n"
                    "Получите новую команду в веб-приложении: Настройки → «Привязать Telegram»."
                )
            return Reply(f"Готово! Аккаунт «{login}» привязан.\n\n{HELP_TEXT}")
        context = await repository.context_for_user(user_id)
        return Reply(HELP_TEXT if context else f"Привет!\n\n{NOT_LINKED_TEXT}")

    if lowered in {"/help", "помощь", "help"}:
        return Reply(HELP_TEXT)

    context = await repository.context_for_user(user_id)
    if context is None:
        return Reply(NOT_LINKED_TEXT)

    if lowered in {"/today", "сегодня", "🍽 сегодня"}:
        meals = await repository.latest_plan_meals(context["household_id"])
        todays = [meal for meal in meals if meal.get("meal_date") == today]
        keyboard = None
        if todays and todays[0].get("plan_id"):
            keyboard = today_keyboard(todays[0]["plan_id"], todays)
        return Reply(format_day(meals, today), keyboard)
    if lowered in {"/week", "неделя", "план", "📅 неделя"}:
        meals = await repository.latest_plan_meals(context["household_id"])
        return Reply(format_week(meals))
    if lowered in {"/shopping", "покупки", "🛒 покупки"}:
        items = await repository.shopping_items(context["household_id"])
        keyboard = None
        if items and items[0].get("plan_id"):
            keyboard = shopping_keyboard(items[0]["plan_id"], items)
        return Reply(format_shopping_header(items, shopping_page(items)), keyboard)

    return Reply(f"Не понял команду.\n\n{HELP_TEXT}")


# --- обработка нажатий кнопок -------------------------------------------------

def _stale() -> CallbackReply:
    return CallbackReply(toast=STALE_TEXT, show_alert=True, edit=Reply(STALE_TEXT))


async def handle_callback(
    app_repository: Any,
    bot_repository: BotRepository,
    user_id: int,
    data: str,
    today: date,
) -> CallbackReply:
    """Единая точка обработки callback_query для всех глаголов.

    ``user_id`` — Telegram ``from.id`` нажавшего (TZ-M7 §3.1).
    """
    parsed = parse_callback(data)
    if parsed is None:
        return CallbackReply(toast="Не понял кнопку.")
    verb, parts = parsed

    # счётчик страниц «2/5» — не кнопка, а подпись: гасим спиннер и всё
    if verb == "n" and parts[:1] == ["noop"]:
        return CallbackReply()

    context = await bot_repository.context_for_user(user_id)
    if context is None:
        return CallbackReply(toast=NOT_LINKED_TEXT, show_alert=True)
    session = bot_session(context)

    try:
        if verb == "p":
            return await _turn_page(app_repository, session, parts)

        plan_id = unpack_uuid(parts[0]) if parts else None
        if plan_id is None:
            return CallbackReply(toast="Не понял кнопку.")

        if verb == "c":
            return CallbackReply(edit=Reply("Оставили как есть."))

        if verb == "s":
            item_id = unpack_uuid(parts[1]) if len(parts) > 1 else None
            if item_id is None:
                return CallbackReply(toast="Не понял кнопку.")
            plan = await app_repository.get_plan(session, plan_id)
            if plan is None:
                return _stale()
            items = plan.get("shopping") or []
            target = next((item for item in items if str(item.get("id")) == str(item_id)), None)
            if target is None:
                return _stale()
            make_purchased = target.get("purchased_at") is None
            result = await app_repository.mark_purchased(session, plan_id, item_id, make_purchased)
            if result is None:
                return _stale()
            target["purchased_at"] = result.get("purchased_at")
            action = "Куплено" if make_purchased else "Снята отметка"
            page = _page_of_item(items, item_id)
            return CallbackReply(
                toast=f"{action}: {target.get('normalized_name')}",
                edit=Reply(
                    format_shopping_header(items, shopping_page(items, page)),
                    shopping_keyboard(plan_id, items, page),
                ),
            )

        if verb == "r":
            meal_id = unpack_uuid(parts[1]) if len(parts) > 1 else None
            if meal_id is None:
                return CallbackReply(toast="Не понял кнопку.")
            plan = await app_repository.get_plan(session, plan_id)
            if plan is None:
                return _stale()
            meal = next(
                (item for item in plan.get("meals", []) if str(item.get("id")) == str(meal_id)),
                None,
            )
            if meal is None:
                return _stale()
            detail = await app_repository.recipe_detail(
                int(meal["recipe_id"]), session["household_id"]
            )
            if detail is None:
                return CallbackReply(toast="Рецепт недоступен.", show_alert=True)
            return CallbackReply(sends=[Reply(format_recipe(detail, meal))])

        if verb == "x":
            meal_id = unpack_uuid(parts[1]) if len(parts) > 1 else None
            if meal_id is None:
                return CallbackReply(toast="Не понял кнопку.")
            latest = await app_repository.latest_plan(session)
            if latest is None or str(latest.get("id")) != str(plan_id):
                return CallbackReply(
                    toast="Это кнопки старого плана — нажмите 🍽 Сегодня ещё раз.",
                    show_alert=True,
                )
            meal = next(
                (item for item in latest.get("meals", []) if str(item.get("id")) == str(meal_id)),
                None,
            )
            if meal is None:
                return _stale()
            result = await app_repository.replace_meal(session, plan_id, meal_id, None)
            if result is None:
                return _stale()
            alternatives = result.get("alternatives") or []
            title = clean_dish_title(str(meal.get("title") or ""))
            if not alternatives:
                return CallbackReply(
                    edit=Reply(f"Для «{title}» подходящих замен не нашлось.")
                )
            return CallbackReply(
                edit=Reply(
                    f"Чем заменить «{title}»?",
                    alternatives_keyboard(plan_id, meal_id, alternatives),
                )
            )

        if verb == "v":
            meal_id = unpack_uuid(parts[1]) if len(parts) > 1 else None
            recipe_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            if meal_id is None or recipe_id is None:
                return CallbackReply(toast="Не понял кнопку.")
            try:
                plan = await app_repository.replace_meal(session, plan_id, meal_id, recipe_id)
            except ValueError as exc:
                return CallbackReply(edit=Reply(str(exc)))
            if plan is None:
                return _stale()
            new_meal = next(
                (item for item in plan.get("meals", []) if str(item.get("id")) == str(meal_id)),
                None,
            )
            done = "✅ Блюдо заменено."
            if new_meal is not None:
                label = MEAL_LABELS.get(str(new_meal.get("meal_type")), "Блюдо")
                kcal = new_meal.get("estimated_kcal")
                kcal_text = f" · ≈{kcal} ккал" if kcal is not None else ""
                done = f"✅ {label} заменён: {clean_dish_title(str(new_meal.get('title')))}{kcal_text}"
            todays = [item for item in plan.get("meals", []) if item.get("meal_date") == today]
            sends = []
            if todays:
                sends.append(Reply(format_day(plan.get("meals", []), today),
                                   today_keyboard(plan_id, todays)))
            return CallbackReply(edit=Reply(done), sends=sends)
    except PermissionError as exc:
        return CallbackReply(toast=str(exc), show_alert=True)

    return CallbackReply(toast="Не понял кнопку.")


async def _turn_page(app_repository: Any, session: dict, parts: list[str]) -> CallbackReply:
    """Листание длинного списка (глагол ``p``): пока только чек-лист покупок."""
    scope = parts[0] if parts else ""
    if scope != "sh" or len(parts) < 3:
        return CallbackReply(toast="Не понял кнопку.")
    plan_id = unpack_uuid(parts[1])
    page = int(parts[2]) if parts[2].isdigit() else 1
    if plan_id is None:
        return CallbackReply(toast="Не понял кнопку.")
    plan = await app_repository.get_plan(session, plan_id)
    if plan is None:
        return _stale()
    items = plan.get("shopping") or []
    return CallbackReply(
        edit=Reply(
            format_shopping_header(items, shopping_page(items, page)),
            shopping_keyboard(plan_id, items, page),
        )
    )


def _page_of_item(items: list[dict[str, Any]], item_id: Any) -> int:
    """На какой странице чек-листа лежит позиция — чтобы после отметки
    пользователь остался там же, где нажимал."""
    from .render import BUTTONS_PER_PAGE, _to_buy

    buyable = [item for item in _to_buy(items) if item.get("id")]
    for index, item in enumerate(buyable):
        if str(item.get("id")) == str(item_id):
            return index // BUTTONS_PER_PAGE + 1
    return 1
