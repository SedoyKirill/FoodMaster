"""Разбор входящего update: личность, доступ, лимиты частоты (TZ-M7 §2, §3.5).

Здесь начинается путь любого сообщения и любой кнопки. Транспорт (``bot.py``)
знает только про сеть, сцены — только про свой диалог; роутер связывает их и
отвечает на три вопроса: кто это, можно ли ему сейчас и куда это отдать.

Состояние лимитов живёт в памяти процесса — как и у веба (``ratelimit.py``).
У бота один процесс, поэтому оговорка про несколько воркеров здесь не нужна.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from app.web.ratelimit import RateLimiter

#: TZ-M7 §3.5
MESSAGES_PER_MINUTE = 20
CALLBACKS_PER_MINUTE = 60
#: тяжёлые операции (подбор замены, сборка меню) — не чаще раза в 10 секунд
HEAVY_EVERY_SECONDS = 10.0
#: об отказе сообщаем не чаще раза в минуту, иначе флуд порождает флуд в ответ
REFUSAL_EVERY_SECONDS = 60.0

#: приёмка §9.6 ищет в ответе именно эту фразу
TOO_FAST_TEXT = "Слишком часто, подождите немного."
BUSY_TEXT = "Уже работаю, секунду…"


@dataclass(frozen=True)
class Actor:
    """Кто нажал и куда отвечать.

    В личном чате ``user_id`` и ``chat_id`` совпадают, но полагаться на это
    нельзя: доступ к семье проверяется только по ``user_id`` (TZ-M7 §3.1).
    """

    user_id: int
    chat_id: int
    message_id: int | None = None


@dataclass(frozen=True)
class Incoming:
    """Разобранный update: одна структура для текста и для нажатой кнопки."""

    actor: Actor
    kind: str = "text"            # text | callback | group
    text: str = ""
    data: str = ""                # callback_data
    callback_id: str = ""


def parse_update(update: dict) -> Incoming | None:
    """update Bot API → Incoming; None — обрабатывать нечего."""
    callback = update.get("callback_query")
    if callback:
        sender = callback.get("from") or {}
        user_id = sender.get("id")
        data = str(callback.get("data") or "")
        if user_id is None or not data:
            return None
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        actor = Actor(
            user_id=int(user_id),
            chat_id=int(chat_id) if chat_id is not None else 0,
            message_id=message.get("message_id"),
        )
        kind = "callback" if _is_private(chat) else "group"
        return Incoming(actor, kind, data=data, callback_id=str(callback.get("id") or ""))

    message = update.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    text = message.get("text")
    if user_id is None or chat_id is None or not text:
        return None
    actor = Actor(
        user_id=int(user_id),
        chat_id=int(chat_id),
        message_id=message.get("message_id"),
    )
    return Incoming(actor, "text" if _is_private(chat) else "group", text=str(text))


def _is_private(chat: dict) -> bool:
    """Личный чат? Отсутствие поля считаем личкой: Telegram его всегда шлёт."""
    return str(chat.get("type") or "private") == "private"


class Router:
    """Лимиты частоты и защита от параллельных тяжёлых операций.

    Часы инъектируются (как в ``ratelimit.py``, TZ-TESTS §2.5), чтобы тесты
    не спали.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.messages = RateLimiter(MESSAGES_PER_MINUTE, 60.0, clock=clock)
        self.callbacks = RateLimiter(CALLBACKS_PER_MINUTE, 60.0, clock=clock)
        self.heavy = RateLimiter(1, HEAVY_EVERY_SECONDS, clock=clock)
        self.refusals = RateLimiter(1, REFUSAL_EVERY_SECONDS, clock=clock)
        #: одна тяжёлая операция на пользователя, а не на пару (чат, кнопка)
        self.in_flight: set[int] = set()

    def allow_text(self, actor: Actor) -> bool:
        """Можно ли обработать сообщение (TZ-M7 §3.5: 20 в минуту)."""
        return self.messages.hit(str(actor.user_id)) == 0

    def allow_callback(self, actor: Actor) -> bool:
        """Можно ли обработать нажатие (60 в минуту)."""
        return self.callbacks.hit(str(actor.user_id)) == 0

    def refusal_text(self, actor: Actor) -> str | None:
        """Что сказать об отказе — не чаще раза в минуту.

        None означает «промолчать»: отвечать на каждое из сотни сообщений
        флудера значит флудить в ответ. Для нажатий текст нужен всегда, там
        используется ``TOO_FAST_TEXT`` напрямую — иначе у пользователя до
        таймаута крутится спиннер.
        """
        return TOO_FAST_TEXT if self.refusals.hit(str(actor.user_id)) == 0 else None

    def acquire_heavy(self, actor: Actor) -> str | None:
        """Занять слот тяжёлой операции. None — занял, строка — отказ.

        Порядок важен: сначала «уже работаю» (это не ошибка пользователя, а
        двойной клик), и только потом лимит частоты — иначе даблклик съедал бы
        десятисекундное окно.
        """
        if actor.user_id in self.in_flight:
            return BUSY_TEXT
        if self.heavy.hit(str(actor.user_id)) != 0:
            return TOO_FAST_TEXT
        self.in_flight.add(actor.user_id)
        return None

    def release_heavy(self, actor: Actor) -> None:
        self.in_flight.discard(actor.user_id)
