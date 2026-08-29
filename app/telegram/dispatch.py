"""Диспетчеризация: команда или нажатая кнопка → нужная сцена (TZ-M7 §2).

Сюда приходит уже разобранный ввод (``router.parse_update``) и уходит
декларативный ``Reply``/``CallbackReply``. Ни сети, ни SQL: на вход —
репозитории (или стабы в тестах), на выход — что показать человеку.

Раньше на этом месте лежал ``service.py`` — плоский каскад из трёх команд и
фасад совместимости на время переезда. К T9 каскад целиком разошёлся по
сценам, поэтому фасад удалён, а модуль назван по своей единственной работе.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.web.planner import clean_dish_title

from .callbacks import encode_callback, parse_callback, unpack_uuid
from .render import (
    MEAL_LABELS, CallbackReply, HELP_TEXT, NOT_LINKED_TEXT, Reply, STALE_TEXT,
    alternatives_keyboard, build_keyboard, format_day, format_recipe, format_week,
    today_keyboard,
)
from .repository import BotRepository, bot_session
from .scenes import auth
from .scenes import inventory as inventory_scene
from .scenes import plan as plan_scene
from .scenes import products as products_scene
from .scenes import recipes as recipes_scene
from .scenes import settings as settings_scene
from .scenes import shopping as shopping_scene
from .scenes import taste as taste_scene


# --- обработка входящих сообщений --------------------------------------------

async def handle_message(
    repository: BotRepository,
    user_id: int,
    text: str,
    today: date,
    *,
    app_repository: Any,
    dialogs: Any,
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
        # §3.2: аккаунта нет — предлагаем завести его прямо здесь
        return Reply(HELP_TEXT) if context else auth.welcome_reply()

    if lowered in {"/help", "помощь", "help"}:
        return Reply(HELP_TEXT)

    context = await repository.context_for_user(user_id)
    if context is None:
        return auth.welcome_reply()

    if lowered == "/web":
        return await auth.web_login(app_repository, context)
    if lowered == "/unlink":
        return auth.unlink_confirmation(
            await app_repository.has_password(context["user_id"])
        )

    if lowered in {"/today", "сегодня", "🍽 сегодня"}:
        meals = await repository.latest_plan_meals(context["household_id"])
        todays = [meal for meal in meals if meal.get("meal_date") == today]
        keyboard = None
        if todays and todays[0].get("plan_id"):
            keyboard = today_keyboard(todays[0]["plan_id"], todays)
        return Reply(format_day(meals, today), keyboard)

    session = bot_session(context)
    if lowered in {"/new", "➕ составить меню"}:
        return await plan_scene.begin(dialogs, user_id)
    if lowered in {"/plan", "/menu", "меню", "📅 меню"}:
        return await _active_plan_reply(app_repository, session)
    if lowered in {"/week", "неделя", "📅 неделя"}:
        # весь план одним текстом — быстрый взгляд без листания по дням
        meals = await repository.latest_plan_meals(context["household_id"])
        return Reply(format_week(meals))
    if lowered in {"/history", "история", "🗂 история"}:
        return await plan_scene.history_reply(app_repository, session)
    if lowered in {"/shopping", "покупки", "🛒 покупки"}:
        latest = await app_repository.latest_plan(session)
        if latest is None:
            return Reply("🛒 Списка покупок нет — сначала составьте меню.")
        return shopping_scene.overview_reply(latest)
    if lowered in {"/recipes", "рецепты", "📖 рецепты"}:
        return await recipes_scene.begin(dialogs, app_repository, user_id)
    if lowered in {"/inventory", "запасы", "🧊 запасы"}:
        return await inventory_scene.begin(
            dialogs, app_repository, session, user_id, today
        )
    if lowered in {"/products", "продукты", "каталог"}:
        return await products_scene.begin(dialogs, app_repository, user_id)
    if lowered in {"/settings", "настройки", "⚙️ настройки"}:
        return await settings_scene.begin(dialogs, app_repository, session, user_id)
    if lowered in {"/taste", "вкусы"}:
        return await taste_scene.begin(app_repository, dialogs, session, user_id)

    return Reply(f"Не понял команду.\n\n{HELP_TEXT}")


# --- обработка нажатий кнопок -------------------------------------------------

def _stale() -> CallbackReply:
    return CallbackReply(toast=STALE_TEXT, show_alert=True, edit=Reply(STALE_TEXT))


async def _active_plan_reply(app_repository: Any, session: dict[str, Any]) -> Reply:
    """Активный план бота — последний собранный (TZ-M7 А4).

    Выбранный из истории план не хранится в состоянии диалога: любая команда
    и любая кнопка главного меню это состояние очищают (§4.2), так что запись
    не пережила бы и одного нажатия. Идентификатор плана и так едет в каждой
    кнопке, поэтому открытый из истории план листается и правится как обычно.
    """
    latest = await app_repository.latest_plan(session)
    if latest is None:
        return Reply(
            "📅 Плана пока нет.",
            build_keyboard([[{
                "text": "➕ Составить меню",
                "callback_data": encode_callback("n", "pl", "new"),
            }]]),
        )
    return plan_scene.day_reply(latest, 1)


async def handle_callback(
    app_repository: Any,
    bot_repository: BotRepository,
    user_id: int,
    data: str,
    today: date,
    *,
    dialogs: Any = None,
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

    # §3.2: кнопки приветствия жмут те, у кого аккаунта ещё нет,
    # поэтому они разбираются до проверки привязки
    if verb == "n" and parts[:1] == ["link"]:
        return CallbackReply(edit=auth.have_account_reply())
    if verb == "n" and parts[:1] in (["reg"], ["regdef"]):
        if dialogs is None:
            return CallbackReply(toast="Регистрация недоступна.", show_alert=True)
        if await bot_repository.context_for_user(user_id) is not None:
            return CallbackReply(toast="Ваш Telegram уже привязан.", show_alert=True)
        if parts[0] == "regdef":
            reply = await auth.create_account(
                app_repository, bot_repository, dialogs, user_id,
                auth.DEFAULT_HOUSEHOLD_NAME,
            )
        else:
            reply = await auth.begin(dialogs, user_id)
        return CallbackReply(edit=reply)

    context = await bot_repository.context_for_user(user_id)
    if context is None:
        return CallbackReply(toast=NOT_LINKED_TEXT, show_alert=True)
    session = bot_session(context)

    if verb == "y" and parts[:1] == ["unlink"]:
        return await auth.unlink(app_repository, session)

    try:
        # --- мастер и экраны меню (TZ-M7 §5.3–5.4) ---------------------------
        if verb == "n" and parts[:1] == ["pl"]:
            result = await plan_scene.handle_callback(
                app_repository, dialogs, session, user_id, parts, today
            )
            if result is not None:
                return result
        if verb == "o":
            # Тумблеры разных экранов различаются областью в первом поле:
            # «ap» — техника, «pm»/«pl» — профиль планирования. Чипы кухонь в
            # мастере меню идут без области, и когда-то всё, кроме техники,
            # уходило к ним — чужие тумблеры отвечали «кнопка устарела».
            scope = parts[0] if parts else ""
            tail = parts[1] if len(parts) > 1 else ""
            if scope == "ap":
                return await settings_scene.toggle_appliance(
                    app_repository, session, tail
                )
            if scope in {"pm", "pl"}:
                return await settings_scene.toggle_plan_profile(
                    app_repository, session, scope, tail
                )
            return await plan_scene.toggle_cuisine(
                dialogs, app_repository, user_id, scope
            )
        if verb == "y" and parts[:1] == ["pd"]:
            return await plan_scene.delete(
                app_repository, session, parts[1] if len(parts) > 1 else ""
            )
        if verb == "p" and parts[:1] == ["pl"]:
            page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            return CallbackReply(
                edit=await plan_scene.history_reply(app_repository, session, page)
            )
        # --- библиотека рецептов (§5.7) ---------------------------------------
        if verb == "n" and parts[:1] == ["st"]:
            result = await settings_scene.handle_navigation(
                app_repository, dialogs, session, user_id, parts
            )
            if result is not None:
                return result
        if verb == "y" and parts[:1] == ["sp"]:
            return await settings_scene.delete_person(
                app_repository, session, parts[1] if len(parts) > 1 else ""
            )
        if verb == "y" and parts[:1] == ["sr"]:
            return await settings_scene.delete_rule(
                app_repository, session, parts[1] if len(parts) > 1 else ""
            )
        if verb == "n" and parts[:1] == ["ts"]:
            result = await taste_scene.handle_navigation(
                app_repository, dialogs, session, user_id, parts
            )
            if result is not None:
                return result
        if verb == "t":
            return await taste_scene.answer(
                app_repository, dialogs, session, user_id, parts
            )
        if verb == "n" and parts[:1] == ["pr"]:
            result = await products_scene.handle_navigation(
                app_repository, dialogs, user_id, parts
            )
            if result is not None:
                return result
        if verb == "n" and parts[:1] == ["rc"]:
            result = await recipes_scene.handle_navigation(
                app_repository, dialogs, session, user_id, parts
            )
            if result is not None:
                return result
        # карточка из библиотеки адресуется числом, из плана — парой UUID
        if verb == "r" and parts and parts[0].isdigit():
            return await recipes_scene.open_card(
                app_repository, session, int(parts[0]),
                with_prices=len(parts) > 1 and parts[1] == "p",
            )
        if verb == "g" and parts and parts[0].isdigit():
            value = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            return await recipes_scene.rate(
                app_repository, session, int(parts[0]), value
            )
        if verb == "w" and len(parts) > 1 and parts[0].isdigit():
            return await recipes_scene.review(
                app_repository, session, int(parts[0]), parts[1]
            )

        # --- запасы (§5.8) ----------------------------------------------------
        if verb == "i" and parts:
            return await inventory_scene.delete(
                app_repository, dialogs, session, user_id, parts[0], today
            )
        if verb == "y" and parts[:1] == ["inu"]:
            return await inventory_scene.undo_delete(
                app_repository, dialogs, session, user_id, today
            )
        if verb == "y" and parts[:1] == ["inx"]:
            return await inventory_scene.confirm_expired(
                app_repository, dialogs, session, user_id, today
            )

        # --- покупки по разделам магазина (§5.6) ------------------------------
        if verb == "f":
            result = await shopping_scene.handle_filter(app_repository, session, parts)
            if result is not None:
                return result
            result = await recipes_scene.handle_filter(
                app_repository, dialogs, user_id, parts
            )
            if result is not None:
                return result
            result = await inventory_scene.handle_filter(
                app_repository, dialogs, session, user_id, parts, today
            )
            if result is not None:
                return result
            result = await products_scene.handle_filter(
                app_repository, dialogs, user_id, parts
            )
            if result is not None:
                return result
        if verb == "p":
            result = await shopping_scene.handle_page(app_repository, session, parts)
            if result is not None:
                return result
            result = await recipes_scene.handle_page(
                app_repository, dialogs, user_id, parts
            )
            if result is not None:
                return result
            result = await inventory_scene.handle_page(
                app_repository, session, parts, today
            )
            if result is not None:
                return result
            result = await products_scene.handle_page(
                app_repository, dialogs, user_id, parts
            )
            if result is not None:
                return result
            return CallbackReply(toast="Не понял кнопку.")

        plan_id = unpack_uuid(parts[0]) if parts else None
        if plan_id is None:
            return CallbackReply(toast="Не понял кнопку.")

        if verb == "d":
            day_number = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            found = await app_repository.get_plan(session, plan_id)
            if found is None:
                return _stale()
            return CallbackReply(edit=plan_scene.day_reply(found, day_number))

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
            # третий аргумент помнит, откуда пришли: из раздела или из
            # сквозного списка. У кнопок, отправленных до T6, его нет.
            marker = parts[2] if len(parts) > 2 else "c"
            return CallbackReply(
                toast=f"{action}: {target.get('normalized_name')}",
                edit=shopping_scene.item_view(plan, item_id, marker),
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


