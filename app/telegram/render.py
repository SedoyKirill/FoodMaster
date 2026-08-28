"""Тексты, клавиатуры и пагинация бота (TZ-M7 §2, §4.4).

Чистые функции: на вход — данные из репозитория, на выход — готовый текст и
разметка. Лимиты Telegram (4096 символов на сообщение, 100 кнопок на
клавиатуру) соблюдаются здесь **конструктивно** — страницами, а не обрезанием
хвоста: пользователь должен иметь возможность добраться до любой позиции.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Sequence

from app.web.planner import clean_dish_title

from .callbacks import encode_callback, pack_uuid

MEAL_LABELS = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}

HELP_TEXT = (
    "Я — Супостат, враг голода. Показываю меню и список покупок вашей семьи.\n\n"
    "Команды:\n"
    "🍽 Сегодня — меню дня: рецепты и замена блюд по кнопкам\n"
    "📅 Меню — план по дням, история и сборка нового\n"
    "🛒 Покупки — чек-лист по разделам магазина\n"
    "/new — составить меню\n"
    "/web — вход в браузер по одноразовому коду\n"
    "/unlink — отвязать Telegram"
)

NOT_LINKED_TEXT = (
    "Ваш Telegram ещё не привязан к семье.\n\n"
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

#: рабочий лимит на сообщение с запасом к жёстким 4096 Telegram
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


def quantity_text(item: dict[str, Any]) -> str:
    quantity = Decimal(str(item["buy_quantity"])).normalize()
    unit = _UNIT_LABELS.get(str(item.get("unit_code")), str(item.get("unit_code") or ""))
    return f"{quantity:f} {unit}".strip()


def to_buy(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in items
        if item.get("buy_quantity") is not None
        and Decimal(str(item["buy_quantity"])) > 0
    ]


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


# --- пагинация (TZ-M7 §4.4) ---------------------------------------------------

#: карточек на страницу (рецепты, запасы, история планов)
CARDS_PER_PAGE = 8
#: кнопок на страницу чек-листа
BUTTONS_PER_PAGE = 30
#: вариантов замены на страницу — их десять (MEAL_ALTERNATIVES_LIMIT)
ALTERNATIVES_PER_PAGE = 5
#: лимит Bot API на одну клавиатуру
MAX_BUTTONS = 100
#: лимит Bot API на подпись кнопки, с запасом
BUTTON_TEXT_LIMIT = 60


@dataclass(frozen=True)
class Page:
    """Одна страница списка: сами позиции и место в общем ряду."""

    items: list[Any]
    page: int      # 1-based, уже зажатый в допустимый диапазон
    pages: int     # всегда ≥ 1
    total: int

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def paginate(items: Sequence[Any], page: int = 1, per_page: int = CARDS_PER_PAGE) -> Page:
    """Страница списка. Номер зажимается: устаревшая кнопка не должна ронять ответ."""
    total = len(items)
    pages = max(1, -(-total // per_page))  # округление вверх
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * per_page
    return Page(list(items[start:start + per_page]), page, pages, total)


def button_text(text: str, limit: int = BUTTON_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def chunk_buttons(buttons: list[dict], per_row: int = 1) -> list[list[dict]]:
    """Плоский список кнопок → ряды по ``per_row``."""
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def pager_row(scope: str, page: Page, *args: Any) -> list[dict]:
    """Ряд «◀ 2/5 ▶». Одна страница — ряда нет вовсе.

    ``args`` — то, что нужно обработчику, чтобы собрать страницу заново
    (например, упакованный id плана): callback_data выходит ``p:scope:args…:N``.
    """
    if page.pages <= 1:
        return []
    row = []
    if page.has_prev:
        row.append({
            "text": "◀",
            "callback_data": encode_callback("p", scope, *args, page.page - 1),
        })
    row.append({
        "text": f"{page.page}/{page.pages}",
        "callback_data": encode_callback("n", "noop"),
    })
    if page.has_next:
        row.append({
            "text": "▶",
            "callback_data": encode_callback("p", scope, *args, page.page + 1),
        })
    return row


def build_keyboard(rows: list[list[dict]]) -> dict[str, Any] | None:
    """Собрать inline_keyboard. Пусто — None; больше 100 кнопок — ошибка.

    Проверка ловит разработчика, а не пользователя: страницы по 30/8/5 делают
    превышение невозможным, и если оно случилось — забыли пагинацию.
    """
    rows = [row for row in rows if row]
    total = sum(len(row) for row in rows)
    if total > MAX_BUTTONS:
        raise ValueError(f"клавиатура из {total} кнопок при лимите {MAX_BUTTONS}")
    return {"inline_keyboard": rows} if rows else None


# --- inline-клавиатуры --------------------------------------------------------

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
    return build_keyboard(rows)


def alternatives_keyboard(
    plan_id: Any, meal_id: Any, alternatives: list[dict[str, Any]]
) -> dict[str, Any]:
    """Все варианты замены, а не первые три.

    Репозиторий отдаёт десять (``MEAL_ALTERNATIVES_LIMIT``), веб показывает
    десять, а бот резал до трёх. Десять рядов — далеко от лимита в 100 кнопок,
    поэтому страницы здесь не нужны; разбиение по пять из §5.5 приедет вместе
    с причинами выбора и дельтами (T5).
    """
    plan = pack_uuid(plan_id)
    meal = pack_uuid(meal_id)
    rows = []
    for index, alternative in enumerate(alternatives, 1):
        title = clean_dish_title(str(alternative.get("title") or ""))
        if alternative.get("draft"):
            title += " (черновик)"
        page_number = alternative.get("source_page_start")
        page_text = f" · стр. {page_number}" if page_number else ""
        rows.append([{
            "text": button_text(f"{index}. {title}{page_text}"),
            "callback_data": encode_callback("v", plan, meal, int(alternative["recipe_id"])),
        }])
    rows.append([{"text": "✖ Оставить как есть", "callback_data": encode_callback("c", plan)}])
    return build_keyboard(rows) or {"inline_keyboard": []}
