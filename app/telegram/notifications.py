"""Напоминания — то, чего веб не умеет по устройству (TZ-M7 §6).

Браузер молчит, пока в него не зайдут; мессенджер может позвать сам. Отсюда
четыре повода написать первым: утреннее меню, портящиеся продукты, несобранная
закупка накануне старта плана и заканчивающийся план.

Устройство: цикл раз в минуту рядом с long polling. Время — локальное для
семьи (``households.timezone``). Дедупликация — по ``last_sent_on``: даже если
бот перезапускали трижды за утро, письмо уйдёт один раз. Отправку принимаем
параметром, поэтому модуль тестируется без сети.

Вечерний вопрос «что приготовили?» из §6 ждёт ``plan_meals.status`` — это
TZ-M8 §4.1, там же появятся сами события вкуса.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .callbacks import encode_callback, pack_uuid
from .render import MEAL_LABELS, Reply, build_keyboard

log = logging.getLogger("ration.telegram")

MORNING = "morning_menu"
EXPIRING = "expiring"
SHOPPING = "shopping"
PLAN_ENDS = "plan_ends"


@dataclass(frozen=True)
class Kind:
    """Повод написать: когда по умолчанию и включён ли без настройки."""

    code: str
    title: str
    hour: int
    enabled: bool


#: §6: по умолчанию включены утреннее меню и сроки, остальное — по желанию (А6)
KINDS: dict[str, Kind] = {
    MORNING: Kind(MORNING, "🍽 Утреннее меню", 8, True),
    EXPIRING: Kind(EXPIRING, "🧊 Сроки годности", 9, True),
    SHOPPING: Kind(SHOPPING, "🛒 Напоминание о закупке", 18, False),
    PLAN_ENDS: Kind(PLAN_ENDS, "📅 План заканчивается", 12, False),
}

#: за сколько дней предупреждать о сроке
EXPIRY_HORIZON_DAYS = 2
#: ниже этой доли купленного напоминаем о закупке накануне старта плана
SHOPPING_READY_SHARE = 0.5

DEFAULT_TIMEZONE = "Europe/Moscow"


def setting_for(kind: str, stored: dict[str, dict[str, Any]]) -> tuple[bool, int, Any]:
    """Настройка повода: своя строка или умолчание из кода."""
    default = KINDS[kind]
    row = stored.get(kind)
    if row is None:
        return default.enabled, default.hour, None
    return bool(row.get("enabled")), int(row.get("hour") or default.hour), row.get("last_sent_on")


def is_due(kind: str, stored: dict[str, dict[str, Any]], now: datetime) -> bool:
    """Пора ли слать: включено, час настал, сегодня ещё не слали.

    Час — «не раньше», а не «ровно в»: если бот лежал в восемь утра и поднялся
    в десять, меню всё равно придёт, но один раз.
    """
    enabled, hour, last_sent_on = setting_for(kind, stored)
    if not enabled or now.hour < hour:
        return False
    if isinstance(last_sent_on, str):
        last_sent_on = date.fromisoformat(last_sent_on)
    return last_sent_on != now.date()


# --- тексты --------------------------------------------------------------------

def morning_reply(plan: dict[str, Any], today: date) -> Reply | None:
    meals = [meal for meal in (plan.get("meals") or []) if _as_date(meal) == today]
    if not meals:
        return None
    lines = ["🍽 Сегодня в меню:"]
    for meal in meals:
        label = MEAL_LABELS.get(str(meal.get("meal_type")), "Блюдо")
        kcal = meal.get("estimated_kcal")
        kcal_text = f" · ≈{kcal} ккал" if kcal is not None else ""
        lines.append(f"• {label}: {meal.get('title')}{kcal_text}")
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
    return Reply("\n".join(lines), build_keyboard(rows))


def expiring_reply(lots: list[dict[str, Any]], today: date,
                   dishes: dict[str, list[str]] | None = None) -> Reply | None:
    """Что скоро испортится и в каких блюдах плана это нужно (§6)."""
    if not lots:
        return None
    lines = ["🧊 Скоро испортится:"]
    planned = dishes or {}
    for lot in lots:
        expires_on = lot.get("expires_on")
        if isinstance(expires_on, str):
            expires_on = date.fromisoformat(expires_on)
        left = (expires_on - today).days
        when = "просрочен" if left < 0 else ("сегодня" if left == 0 else f"через {left} дн.")
        line = f"• {lot.get('name')} — {when}"
        # где это пригодится: искать блюдо в плане самому неудобно
        used_in = planned.get(str(lot.get("name") or ""))
        if used_in:
            line += f" · в плане: {', '.join(used_in[:2])}"
        lines.append(line)
    rows = [[{"text": "🧊 Запасы", "callback_data": encode_callback("p", "in", 1)}]]
    return Reply("\n".join(lines), build_keyboard(rows))


def shopping_reply(plan: dict[str, Any]) -> Reply | None:
    """Напоминание накануне старта плана, если куплено меньше половины."""
    items = [
        item for item in (plan.get("shopping") or [])
        if item.get("buy_quantity") and float(item["buy_quantity"]) > 0
    ]
    if not items:
        return None
    left = [item for item in items if item.get("purchased_at") is None]
    if not left:
        return None
    if 1 - len(left) / len(items) >= SHOPPING_READY_SHARE:
        return None  # большая часть уже куплена — напоминать не о чем
    total_kop = sum(int(item.get("estimated_cost_kop") or 0) for item in left)
    money = f" на {total_kop // 100} ₽" if total_kop else ""
    plan_id = pack_uuid(plan["id"])
    return Reply(
        f"🛒 Завтра начинается меню, а не куплено {len(left)} из {len(items)} позиций{money}.",
        build_keyboard([[{
            "text": "🛒 Покупки",
            "callback_data": encode_callback("f", "sh", plan_id, ""),
        }]]),
    )


def plan_ends_reply(plan: dict[str, Any], today: date) -> Reply | None:
    """Последний день плана — предложить собрать следующий."""
    starts_on = _as_date(plan, "starts_on")
    days = int(plan.get("days") or 0)
    if starts_on is None or days <= 0:
        return None
    if starts_on + timedelta(days=days - 1) != today:
        return None
    return Reply(
        "📅 Сегодня последний день меню. Составить на следующую неделю?",
        build_keyboard([[{
            "text": "➕ Составить меню",
            "callback_data": encode_callback("n", "pl", "new"),
        }]]),
    )


def _plan_uuid(value: Any) -> Any:
    return value if isinstance(value, uuid_mod.UUID) else uuid_mod.UUID(str(value))


def _as_date(source: dict[str, Any], key: str = "meal_date") -> date | None:
    value = source.get(key)
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value if isinstance(value, date) else None


# --- цикл ----------------------------------------------------------------------

class Notifier:
    """Раз в минуту смотрит, кому пора написать, и пишет.

    ``send`` приходит из транспорта: модуль ничего не знает про Bot API, и
    поэтому проверяется без сети. Часы инъектируются, как в ``ratelimit``.
    """

    def __init__(
        self,
        bot_repository: Any,
        app_repository: Any,
        send: Callable[[int, Reply], Awaitable[Any]],
        *,
        clock: Callable[[], datetime] = datetime.now,
        default_timezone: str = DEFAULT_TIMEZONE,
    ) -> None:
        self.bot_repository = bot_repository
        self.app_repository = app_repository
        self.send = send
        self.clock = clock
        self.default_timezone = default_timezone

    def _now_for(self, target: dict[str, Any]) -> datetime:
        name = str(target.get("timezone") or self.default_timezone)
        try:
            zone = ZoneInfo(name)
        except Exception:  # noqa: BLE001 — часовой пояс из БД может быть мусором
            zone = ZoneInfo(self.default_timezone)
        moment = self.clock()
        if moment.tzinfo is None:
            return moment
        return moment.astimezone(zone)

    async def tick(self) -> int:
        """Один проход по всем привязанным. Возвращает число отправленных."""
        sent = 0
        for target in await self.bot_repository.notification_targets():
            try:
                sent += await self._notify(target)
            except Exception:  # noqa: BLE001 — один сломанный адресат не должен
                log.exception("Не удалось разобрать напоминания для %s",
                              target.get("telegram_id"))
        return sent

    async def _notify(self, target: dict[str, Any]) -> int:
        telegram_id = int(target["telegram_id"])
        now = self._now_for(target)
        stored = await self.bot_repository.notification_settings(telegram_id)
        due = [kind for kind in KINDS if is_due(kind, stored, now)]
        if not due:
            return 0

        session = {
            "household_id": target["household_id"], "user_id": target["user_id"],
            "role": target.get("role"), "login": target.get("login"),
            "household_name": target.get("household_name"), "channel": "telegram",
        }
        plan = await self.app_repository.latest_plan(session)
        sent = 0
        for kind in due:
            reply = await self._build(kind, session, plan, now.date(), target)
            if reply is None:
                continue
            await self.send(telegram_id, reply)
            await self.bot_repository.mark_notified(telegram_id, kind, now.date())
            sent += 1
        return sent

    async def _build(self, kind: str, session: dict[str, Any], plan: dict[str, Any] | None,
                     today: date, target: dict[str, Any]) -> Reply | None:
        if kind == EXPIRING:
            lots = await self.bot_repository.expiring_lots(
                target["household_id"], today + timedelta(days=EXPIRY_HORIZON_DAYS)
            )
            dishes = {}
            if lots and plan is not None:
                dishes = await self.bot_repository.dishes_using(
                    _plan_uuid(plan["id"]), [str(lot.get("name")) for lot in lots]
                )
            return expiring_reply(lots, today, dishes)
        if plan is None:
            return None
        if kind == MORNING:
            return morning_reply(plan, today)
        if kind == PLAN_ENDS:
            return plan_ends_reply(plan, today)
        if kind == SHOPPING:
            starts_on = _as_date(plan, "starts_on")
            if starts_on != today + timedelta(days=1):
                return None
            return shopping_reply(plan)
        return None

    async def run(self, interval: float = 60.0) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — цикл напоминаний не роняет бота
                log.exception("Сбой цикла напоминаний")
            await asyncio.sleep(interval)
