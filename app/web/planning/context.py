"""Контекст горизонта: календарь, сезон, история и время готовки (TZ-M8 §3.5–3.7).

Планировщик до M8 не отличал среду от субботы, август от февраля и не помнил,
что ели на прошлой неделе: в план на понедельник спокойно попадало блюдо на
два часа, в феврале — салат из кабачков, а вчерашний суп мог встретиться
снова. Всё, что тут считается, — арифметика и константы: ни внешних сервисов,
ни LLM в runtime (TZ-v2 §3).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

#: сколько дней истории учитывает ротация
HISTORY_WINDOW_DAYS = 21
#: любимое блюдо (аффинити от этого порога) возвращается уже через неделю
FAVOURITE_AFFINITY = 0.8
FAVOURITE_WINDOW_DAYS = 6

#: Государственные праздники РФ (день, месяц). Переносы, когда праздник
#: выпадает на выходной, считаются базовым правилом «следующий рабочий день»
#: — точные переносы каждый год утверждает постановление правительства, но
#: планировщику важно лишь «дома есть время готовить или нет».
PUBLIC_HOLIDAYS = (
    (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1), (8, 1),  # новогодние
    (23, 2),   # День защитника Отечества
    (8, 3),    # Международный женский день
    (1, 5),    # Праздник Весны и Труда
    (9, 5),    # День Победы
    (12, 6),   # День России
    (4, 11),   # День народного единства
)

#: Сезонные продукты средней полосы по месяцам. Нужны вкусу и разнообразию:
#: цены сезона каталог Ленты отражает сам.
SEASONAL_INGREDIENTS: dict[int, frozenset[str]] = {
    1: frozenset({"квашеная капуста", "капуста", "свёкла", "морковь", "тыква", "апельсин",
                  "мандарин", "лимон", "хурма", "картофель", "лук", "яблоко"}),
    2: frozenset({"квашеная капуста", "капуста", "свёкла", "морковь", "тыква", "апельсин",
                  "мандарин", "лимон", "картофель", "лук", "яблоко"}),
    3: frozenset({"капуста", "свёкла", "морковь", "лук", "картофель", "апельсин", "лимон",
                  "зелёный лук", "шпинат"}),
    4: frozenset({"редис", "щавель", "шпинат", "зелёный лук", "укроп", "спаржа", "капуста",
                  "морковь"}),
    5: frozenset({"редис", "щавель", "шпинат", "зелёный лук", "укроп", "спаржа", "огурец",
                  "клубника", "ревень", "салат"}),
    6: frozenset({"клубника", "черешня", "огурец", "редис", "укроп", "петрушка", "салат",
                  "кабачок", "зелёный горошек", "молодой картофель"}),
    7: frozenset({"кабачок", "огурец", "томат", "черника", "малина", "смородина", "вишня",
                  "укроп", "базилик", "перец", "баклажан", "абрикос"}),
    8: frozenset({"кабачок", "огурец", "томат", "баклажан", "перец", "слива", "яблоко",
                  "груша", "кукуруза", "укроп", "базилик", "малина", "арбуз", "дыня"}),
    9: frozenset({"тыква", "кабачок", "томат", "баклажан", "перец", "яблоко", "груша",
                  "слива", "виноград", "капуста", "гриб", "свёкла", "морковь"}),
    10: frozenset({"тыква", "капуста", "свёкла", "морковь", "картофель", "гриб", "яблоко",
                   "груша", "клюква", "брусника", "лук"}),
    11: frozenset({"тыква", "капуста", "квашеная капуста", "свёкла", "морковь", "картофель",
                   "клюква", "хурма", "мандарин", "лук"}),
    12: frozenset({"квашеная капуста", "капуста", "свёкла", "морковь", "тыква", "мандарин",
                   "апельсин", "хурма", "картофель", "лук", "яблоко"}),
}

SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}


@dataclass(frozen=True)
class DayContext:
    """Что планировщик знает про конкретный день горизонта."""

    day: date
    weekday: bool
    holiday: bool
    season: str


def is_holiday(day: date) -> bool:
    """Нерабочий праздничный день, включая перенос с выходного."""
    if (day.day, day.month) in PUBLIC_HOLIDAYS:
        return True
    # Праздник выпал на субботу или воскресенье — отдыхаем в понедельник.
    for shift in (1, 2):
        earlier = day - timedelta(days=shift)
        if (earlier.day, earlier.month) in PUBLIC_HOLIDAYS and earlier.weekday() >= 5:
            return day.weekday() < 5
    return False


def season_of(day: date) -> str:
    return SEASONS[day.month]


def day_context(day: date) -> DayContext:
    """Будни/выходной/праздник и сезон одного дня."""
    holiday = is_holiday(day)
    return DayContext(
        day=day,
        weekday=day.weekday() < 5 and not holiday,
        holiday=holiday,
        season=season_of(day),
    )


def season_share(ingredients: Iterable[str], day: date) -> float:
    """Доля сезонных продуктов в блюде — мягкий бонус, а не фильтр."""
    names = [str(name).strip().casefold() for name in ingredients if str(name).strip()]
    if not names:
        return 0.0
    seasonal = SEASONAL_INGREDIENTS[day.month]
    hits = sum(
        1 for name in names if name in seasonal or any(word in seasonal for word in name.split())
    )
    return hits / len(names)


def slot_time_limit(
    meal_type: str, context: DayContext, profile: dict[str, Any]
) -> int | None:
    """Сколько минут семья готова готовить в этот слот; None — без лимита.

    Выходной и праздник живут по одному правилу: в субботу и 9 мая время на
    кухне есть, в среду вечером его нет.
    """
    if not context.weekday:
        return profile.get("weekend_max_minutes")
    if meal_type == "breakfast":
        return profile.get("breakfast_max_minutes")
    return profile.get("weekday_max_minutes")


@dataclass(frozen=True)
class PlanHistory:
    """Что семья ела в последние три недели (TZ-M8 §3.7)."""

    last_seen: dict[int, int] = field(default_factory=dict)
    recent_dish_types: Counter = field(default_factory=Counter)
    recent_main_ingredients: Counter = field(default_factory=Counter)

    @classmethod
    def empty(cls) -> "PlanHistory":
        return cls()

    def days_since(self, recipe_id: int) -> int | None:
        """Сколько дней назад блюдо было в плане; None — не встречалось."""
        return self.last_seen.get(int(recipe_id))

    def recency_penalty(self, recipe_id: int, affinity: float = 0.0) -> float:
        """0 — давно не ели, 1 — ели вчера.

        Любимому блюду скидка: его штрафует только неделя, а не три —
        иначе борщ, который семья просит каждую субботу, исчез бы из меню.
        """
        days = self.days_since(recipe_id)
        if days is None:
            return 0.0
        window = FAVOURITE_WINDOW_DAYS if affinity >= FAVOURITE_AFFINITY else HISTORY_WINDOW_DAYS
        if days > window:
            return 0.0
        return max(0.0, (HISTORY_WINDOW_DAYS - days) / HISTORY_WINDOW_DAYS)


def _as_date(value: Any) -> date | None:
    """Дата из строки плана.

    Репозиторий отдаёт строки через ``row_dict``, а тот приводит даты к ISO —
    иначе они не сериализуются в JSON. История принимала только ``date`` и
    молча отбрасывала всё: ротация не работала ни разу с самого T4.
    """
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def build_history(rows: list[dict[str, Any]], starts_on: date) -> PlanHistory:
    """История из строк ``plan_meals`` за окно до начала плана."""
    last_seen: dict[int, int] = {}
    dish_types: Counter = Counter()
    mains: Counter = Counter()
    for row in rows:
        meal_date = _as_date(row.get("meal_date"))
        if meal_date is None:
            continue
        days = (starts_on - meal_date).days
        if days < 0 or days > HISTORY_WINDOW_DAYS:
            continue
        recipe_id = int(row["recipe_id"])
        previous = last_seen.get(recipe_id)
        if previous is None or days < previous:
            last_seen[recipe_id] = days
        if row.get("dish_type"):
            dish_types[str(row["dish_type"])] += 1
        if row.get("main_ingredient"):
            mains[str(row["main_ingredient"])] += 1
    return PlanHistory(last_seen, dish_types, mains)
