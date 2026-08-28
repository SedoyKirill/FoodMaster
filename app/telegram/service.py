"""Логика Telegram-бота: привязка чата, выборки, клавиатуры и тексты ответов.

Никакого сетевого кода — всё принимает пул asyncpg / AppRepository (или стабы
в тестах) и возвращает декларативные Reply/CallbackReply для транспорта.
"""

from __future__ import annotations

import base64
import uuid as uuid_mod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.web.planner import clean_dish_title
from app.web.security import token_hash

MEAL_LABELS = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}

HELP_TEXT = (
    "Я — Супостат, враг голода. Показываю меню и список покупок вашей семьи.\n\n"
    "Команды:\n"
    "🍽 Сегодня — меню дня: рецепты и замена блюд по кнопкам\n"
    "📅 Неделя — весь текущий план\n"
    "🛒 Покупки — чек-лист: жмите на позицию, чтобы отметить купленное\n\n"
    "Планы составляются в веб-приложении «Рацион»."
)

NOT_LINKED_TEXT = (
    "Этот чат ещё не привязан к семье.\n\n"
    "Откройте веб-приложение «Рацион» → Настройки → «Привязать Telegram», "
    "получите команду вида «/start link_…» и отправьте её мне в течение 10 минут."
)

STALE_TEXT = (
    "Данные обновились, кнопки устарели — нажмите 🛒 Покупки или 🍽 Сегодня ещё раз."
)


# --- результаты обработчиков -------------------------------------------------

@dataclass
class Reply:
    """Одно сообщение: текст + опциональная inline-клавиатура."""

    text: str
    keyboard: dict[str, Any] | None = None


@dataclass
class CallbackReply:
    """Эффекты обработки нажатия кнопки."""

    toast: str = ""
    show_alert: bool = False
    edit: Reply | None = None          # перерисовать сообщение с кнопкой
    sends: list[Reply] = field(default_factory=list)  # новые сообщения


# --- кодек callback_data (≤ 64 байта) ---------------------------------------

def pack_uuid(value: Any) -> str:
    """UUID → 22 символа base64url (без «=»)."""
    value = value if isinstance(value, uuid_mod.UUID) else uuid_mod.UUID(str(value))
    return base64.urlsafe_b64encode(value.bytes).rstrip(b"=").decode("ascii")


def unpack_uuid(text: str) -> uuid_mod.UUID | None:
    try:
        raw = base64.urlsafe_b64decode(text + "==")
        return uuid_mod.UUID(bytes=raw)
    except (ValueError, TypeError):
        return None


def encode_callback(verb: str, *parts: Any) -> str:
    encoded = "|".join([verb, *[str(part) for part in parts]])
    assert len(encoded.encode("utf-8")) <= 64, f"callback_data длиннее 64 байт: {encoded!r}"
    return encoded


def parse_callback(data: str) -> tuple[str, list[str]] | None:
    if not data or "|" not in data:
        return None
    verb, *parts = data.split("|")
    if verb not in {"s", "r", "x", "v", "c"}:
        return None
    return verb, parts


def callback_verb(data: str) -> str:
    parsed = parse_callback(data or "")
    return parsed[0] if parsed else ""


# --- запросы бота к базе ------------------------------------------------------

class BotRepository:
    """Свои запросы бота: привязка чата и лёгкие выборки для сообщений."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def link_chat(self, chat_id: int, raw_token: str) -> str | None:
        """Погасить одноразовый токен и привязать чат. Возвращает login или None."""
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE app_core.one_time_tokens
                SET used_at = CURRENT_TIMESTAMP
                WHERE token_hash=$1 AND purpose='telegram_link'
                  AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                RETURNING user_id
                """,
                token_hash(raw_token),
            )
            if not row:
                return None
            user_id = row["user_id"]
            await connection.execute(
                """
                DELETE FROM app_core.auth_identities
                WHERE provider='telegram' AND (provider_user_id=$1 OR user_id=$2)
                """,
                str(chat_id), user_id,
            )
            await connection.execute(
                """
                INSERT INTO app_core.auth_identities (provider, provider_user_id, user_id)
                VALUES ('telegram', $1, $2)
                """,
                str(chat_id), user_id,
            )
            login = await connection.fetchval(
                "SELECT login FROM app_core.users WHERE id=$1", user_id
            )
            return str(login or "")

    async def context_for_chat(self, chat_id: int) -> dict[str, Any] | None:
        """Пользователь и семья по чату; None — чат не привязан."""
        row = await self.pool.fetchrow(
            """
            SELECT u.id AS user_id, u.login, m.household_id, m.role
            FROM app_core.auth_identities ai
            JOIN app_core.users u ON u.id = ai.user_id AND u.status='active'
            JOIN app_core.household_memberships m ON m.user_id = u.id
            WHERE ai.provider='telegram' AND ai.provider_user_id=$1
            ORDER BY m.created_at
            LIMIT 1
            """,
            str(chat_id),
        )
        return dict(row) if row else None

    async def latest_plan_meals(self, household_id: Any) -> list[dict[str, Any]]:
        """Блюда последнего плана семьи (весь горизонт), с id для кнопок."""
        rows = await self.pool.fetch(
            """
            SELECT pm.id, pm.plan_id, pm.meal_date, pm.meal_type, pm.position,
                   pm.recipe_id, pm.estimated_kcal, pm.estimated_protein,
                   pm.estimated_fat, pm.estimated_carb, r.title
            FROM app_core.plan_meals pm
            JOIN recipe_library.recipes r ON r.id = pm.recipe_id
            WHERE pm.plan_id = (
                SELECT id FROM app_core.meal_plans
                WHERE household_id=$1 ORDER BY created_at DESC LIMIT 1
            )
            ORDER BY pm.meal_date, pm.position
            """,
            household_id,
        )
        return [dict(row) for row in rows]

    async def shopping_items(self, household_id: Any) -> list[dict[str, Any]]:
        """Список покупок последнего плана семьи, с id для кнопок."""
        rows = await self.pool.fetch(
            """
            SELECT pi.id, pi.plan_id, pi.normalized_name, pi.buy_quantity,
                   pi.unit_code, pi.pack_count, pi.estimated_cost_kop,
                   pi.purchased_at, pi.to_taste
            FROM app_core.plan_ingredients pi
            WHERE pi.plan_id = (
                SELECT id FROM app_core.meal_plans
                WHERE household_id=$1 ORDER BY created_at DESC LIMIT 1
            )
            ORDER BY pi.normalized_name
            """,
            household_id,
        )
        return [dict(row) for row in rows]


# --- форматирование (чистые функции) -----------------------------------------

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

_UNIT_LABELS = {
    "g": "г", "kg": "кг", "ml": "мл", "l": "л", "piece": "шт", "tbsp": "ст. л.",
    "tsp": "ч. л.", "cup": "стакан", "bunch": "пучок", "clove": "зубчик",
    "pinch": "щепотка", "slice": "ломтик", "can": "банка", "pack": "упаковка",
}

TELEGRAM_LIMIT = 4000


def _date_label(value: date) -> str:
    return f"{_WEEKDAYS[value.weekday()]}, {value.day} {_MONTHS[value.month - 1]}"


def _macros_text(meal: dict[str, Any]) -> str:
    if meal.get("estimated_protein") is None:
        return ""
    return (
        f" · Б/Ж/У {meal['estimated_protein']}/{meal['estimated_fat']}"
        f"/{meal['estimated_carb']} г"
    )


def _meal_line(meal: dict[str, Any]) -> str:
    title = clean_dish_title(str(meal.get("title") or "Блюдо"))
    label = MEAL_LABELS.get(str(meal.get("meal_type")), str(meal.get("meal_type")))
    kcal = meal.get("estimated_kcal")
    kcal_text = f" · ≈{kcal} ккал" if kcal is not None else ""
    return f"• {label}: {title}{kcal_text}{_macros_text(meal)}"


def format_day(meals: list[dict[str, Any]], day: date) -> str:
    todays = [meal for meal in meals if meal.get("meal_date") == day]
    if not todays:
        return (
            f"🍽 На {_date_label(day)} блюд в плане нет.\n"
            "Загляните в веб-приложение и составьте новый план."
        )
    lines = [f"🍽 Меню на {_date_label(day)}:"] + [_meal_line(meal) for meal in todays]
    known = [meal["estimated_kcal"] for meal in todays if meal.get("estimated_kcal") is not None]
    if known:
        suffix = "" if len(known) == len(todays) else f" (по {len(known)} из {len(todays)} блюд)"
        lines.append(f"Итого ≈{sum(known)} ккал{suffix}")
    return "\n".join(lines)


def format_week(meals: list[dict[str, Any]]) -> str:
    if not meals:
        return "📅 Плана пока нет — составьте его в веб-приложении «Рацион»."
    lines = ["📅 Текущий план:"]
    current: date | None = None
    for meal in meals:
        meal_date = meal.get("meal_date")
        if meal_date != current:
            current = meal_date
            lines.append("")
            lines.append(_date_label(meal_date) if isinstance(meal_date, date) else str(meal_date))
        lines.append(_meal_line(meal))
    return "\n".join(lines)


def _quantity_text(item: dict[str, Any]) -> str:
    quantity = Decimal(str(item["buy_quantity"])).normalize()
    unit = _UNIT_LABELS.get(str(item.get("unit_code")), str(item.get("unit_code") or ""))
    return f"{quantity:f} {unit}".strip()


def _to_buy(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in items
        if item.get("buy_quantity") is not None
        and Decimal(str(item["buy_quantity"])) > 0
    ]


def format_shopping_header(items: list[dict[str, Any]]) -> str:
    buyable = _to_buy(items)
    if not items:
        return "🛒 Списка покупок нет — сначала составьте план."
    remaining = [item for item in buyable if item.get("purchased_at") is None]
    if not remaining:
        return "🛒 Всё куплено. Отличная работа!"
    total_kop = sum(int(item.get("estimated_cost_kop") or 0) for item in remaining)
    total = f" ≈{total_kop / 100:.0f} ₽" if total_kop else ""
    return (
        f"🛒 Осталось купить {len(remaining)} из {len(buyable)} позиций{total}.\n"
        "Нажимайте на позиции, чтобы отметить купленное (повторное нажатие снимает отметку)."
    )


def format_shopping(items: list[dict[str, Any]]) -> str:
    """Плоский текстовый список (используется, когда кнопки не нужны)."""
    buyable = _to_buy(items)
    if not items:
        return "🛒 Списка покупок нет — сначала составьте план."
    remaining = [item for item in buyable if item.get("purchased_at") is None]
    if not remaining:
        return "🛒 Всё куплено. Отличная работа!"
    lines = ["🛒 Осталось купить:"]
    total_kop = 0
    for item in remaining:
        packs = item.get("pack_count")
        pack_text = f" ({packs} уп.)" if packs else ""
        cost = item.get("estimated_cost_kop")
        cost_text = ""
        if cost is not None:
            total_kop += int(cost)
            cost_text = f" — {int(cost) / 100:.0f} ₽"
        lines.append(f"• {item['normalized_name']}: {_quantity_text(item)}{pack_text}{cost_text}")
    if total_kop:
        lines.append(f"Итого ≈{total_kop / 100:.0f} ₽")
    return "\n".join(lines)


def format_recipe(detail: dict[str, Any], meal: dict[str, Any] | None = None) -> str:
    """Карточка рецепта: КБЖУ берём из блюда плана (уже в масштабе семьи)."""
    lines = [f"📖 {clean_dish_title(str(detail.get('title') or 'Рецепт'))}"]
    meta = []
    if detail.get("source_page_start"):
        meta.append(f"стр. {detail['source_page_start']}")
    if detail.get("source_servings_min"):
        meta.append(f"порций в книге: {detail['source_servings_min']}")
    if detail.get("time_total_minutes"):
        meta.append(f"~{detail['time_total_minutes']} мин")
    if meta:
        lines.append(" · ".join(str(part) for part in meta))
    if meal is not None and meal.get("estimated_kcal") is not None:
        lines.append(
            f"≈{meal['estimated_kcal']} ккал на всё блюдо{_macros_text(meal)}"
        )
    ingredients = detail.get("ingredients") or []
    if ingredients:
        lines.append("")
        lines.append("Ингредиенты:")
        for ingredient in ingredients:
            text = str(
                ingredient.get("raw_text")
                or ingredient.get("ingredient_text")
                or ingredient.get("normalized_name")
                or ""
            ).strip()
            if ingredient.get("is_to_taste") and "вкус" not in text.lower():
                text += " — по вкусу"
            lines.append(f"• {text}")
    steps = detail.get("steps") or []
    if steps:
        lines.append("")
        lines.append("Приготовление:")
        for step in steps:
            lines.append("")
            lines.append(f"{step.get('position')}. {str(step.get('instruction') or '').strip()}")
    return "\n".join(lines)


def split_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Резка длинного текста по абзацам → строкам → жёсткому срезу."""
    if len(text) <= limit:
        return [text] if text else []

    def _split_units(units: list[str], separator: str) -> list[str]:
        chunks: list[str] = []
        current = ""
        for unit in units:
            candidate = f"{current}{separator}{unit}" if current else unit
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
            while len(unit) > limit:
                chunks.append(unit[:limit])
                unit = unit[limit:]
            current = unit
        if current:
            chunks.append(current)
        return chunks

    paragraphs = text.split("\n\n")
    safe_units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) > limit:
            safe_units.extend(_split_units(paragraph.split("\n"), "\n"))
        else:
            safe_units.append(paragraph)
    return [chunk for chunk in _split_units(safe_units, "\n\n") if chunk.strip()]


# --- inline-клавиатуры --------------------------------------------------------

MAX_SHOPPING_BUTTONS = 90  # лимит Bot API — 100 кнопок на сообщение
_BUTTON_TEXT_LIMIT = 60


def today_keyboard(plan_id: Any, meals: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = []
    plan = pack_uuid(plan_id)
    for meal in meals:
        if not meal.get("id"):
            continue
        label = MEAL_LABELS.get(str(meal.get("meal_type")), "Блюдо")
        packed = pack_uuid(meal["id"])
        rows.append([
            {"text": f"📖 {label}", "callback_data": encode_callback("r", plan, packed)},
            {"text": f"🔁 {label}", "callback_data": encode_callback("x", plan, packed)},
        ])
    return {"inline_keyboard": rows} if rows else None


def shopping_keyboard(plan_id: Any, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = []
    plan = pack_uuid(plan_id)
    for item in _to_buy(items)[:MAX_SHOPPING_BUTTONS]:
        if not item.get("id"):
            continue
        mark = "✅" if item.get("purchased_at") else "☐"
        cost = item.get("estimated_cost_kop")
        cost_text = f" — {int(cost) / 100:.0f} ₽" if cost else ""
        text = f"{mark} {item['normalized_name']} · {_quantity_text(item)}{cost_text}"
        if len(text) > _BUTTON_TEXT_LIMIT:
            text = text[:_BUTTON_TEXT_LIMIT - 1] + "…"
        rows.append([
            {"text": text, "callback_data": encode_callback("s", plan, pack_uuid(item["id"]))}
        ])
    return {"inline_keyboard": rows} if rows else None


def alternatives_keyboard(
    plan_id: Any, meal_id: Any, alternatives: list[dict[str, Any]]
) -> dict[str, Any]:
    plan = pack_uuid(plan_id)
    meal = pack_uuid(meal_id)
    rows = []
    for index, alternative in enumerate(alternatives[:3], 1):
        title = clean_dish_title(str(alternative.get("title") or ""))
        if alternative.get("draft"):
            title += " (черновик)"
        page = alternative.get("source_page_start")
        page_text = f" · стр. {page}" if page else ""
        text = f"{index}. {title}{page_text}"
        if len(text) > _BUTTON_TEXT_LIMIT:
            text = text[:_BUTTON_TEXT_LIMIT - 1] + "…"
        rows.append([{
            "text": text,
            "callback_data": encode_callback("v", plan, meal, int(alternative["recipe_id"])),
        }])
    rows.append([{"text": "✖ Оставить как есть", "callback_data": encode_callback("c", plan)}])
    return {"inline_keyboard": rows}


# --- обработка входящих сообщений --------------------------------------------

async def handle_message(
    repository: BotRepository, chat_id: int, text: str, today: date
) -> Reply:
    """Ответ на текстовое сообщение. Не бросает — ошибки ловит транспорт."""
    text = (text or "").strip()
    lowered = text.lower()

    if lowered.startswith("/start"):
        payload = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if payload.startswith("link_"):
            login = await repository.link_chat(chat_id, payload[len("link_"):])
            if login is None:
                return Reply(
                    "Ссылка привязки не подошла: токен просрочен или уже использован.\n"
                    "Получите новую команду в веб-приложении: Настройки → «Привязать Telegram»."
                )
            return Reply(f"Готово! Чат привязан к аккаунту «{login}».\n\n{HELP_TEXT}")
        context = await repository.context_for_chat(chat_id)
        return Reply(HELP_TEXT if context else f"Привет!\n\n{NOT_LINKED_TEXT}")

    if lowered in {"/help", "помощь", "help"}:
        return Reply(HELP_TEXT)

    context = await repository.context_for_chat(chat_id)
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
        return Reply(format_shopping_header(items), keyboard)

    return Reply(f"Не понял команду.\n\n{HELP_TEXT}")


# --- обработка нажатий кнопок -------------------------------------------------

def _stale() -> CallbackReply:
    return CallbackReply(toast=STALE_TEXT, show_alert=True, edit=Reply(STALE_TEXT))


async def handle_callback(
    app_repository: Any,
    bot_repository: BotRepository,
    chat_id: int,
    data: str,
    today: date,
) -> CallbackReply:
    """Единая точка обработки callback_query для всех глаголов."""
    parsed = parse_callback(data)
    if parsed is None:
        return CallbackReply(toast="Не понял кнопку.")
    verb, parts = parsed

    context = await bot_repository.context_for_chat(chat_id)
    if context is None:
        return CallbackReply(toast=NOT_LINKED_TEXT, show_alert=True)
    session = {
        "household_id": context["household_id"],
        "user_id": context["user_id"],
        "role": context["role"],
    }

    plan_id = unpack_uuid(parts[0]) if parts else None
    if plan_id is None:
        return CallbackReply(toast="Не понял кнопку.")

    try:
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
            return CallbackReply(
                toast=f"{action}: {target.get('normalized_name')}",
                edit=Reply(format_shopping_header(items), shopping_keyboard(plan_id, items)),
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
