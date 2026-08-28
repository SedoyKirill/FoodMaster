"""Транспорт Telegram-бота: long polling Bot API без внешних фреймворков.

Запуск: ``python -m app.telegram.bot`` (нужны TELEGRAM_BOT_TOKEN и DATABASE_URL).
Логика ответов — в ``service.py``; здесь только сеть и диспетчеризация.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
import httpx

from app.web.database import AppRepository
from app.web.ratelimit import RateLimiter

from .repository import BotRepository
from .router import TOO_FAST_TEXT, Actor, Incoming, Router, parse_update
from .service import (
    CallbackReply, Reply, callback_verb, handle_callback, handle_message,
    split_for_telegram,
)

log = logging.getLogger("ration.telegram")

#: постоянная reply-клавиатура с основными командами
KEYBOARD = {
    "keyboard": [
        [{"text": "🍽 Сегодня"}, {"text": "📅 Неделя"}],
        [{"text": "🛒 Покупки"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

POLL_TIMEOUT_SECONDS = 50
ERROR_BACKOFF_SECONDS = 5
#: часовой пояс семьи — тот же, что у ночного сборщика
TIMEZONE = ZoneInfo(os.getenv("LENTA_TIMEZONE", "Europe/Moscow"))
#: тяжёлые глаголы уходят в фоновую задачу, чтобы не морозить конвейер
HEAVY_VERBS = {"x", "v"}
#: приёмка TZ-M7 §9.9 — «⏳» не живёт дольше минуты; запас на доставку правки
HEAVY_TIMEOUT_SECONDS = 45.0

#: TZ-M7 §4.1. Список растёт по мере появления сцен (T4–T9): рекламировать
#: команды, которые ещё не работают, хуже, чем не показывать их вовсе.
BOT_COMMANDS = [
    ("start", "Начать и привязать аккаунт"),
    ("today", "Меню на сегодня"),
    ("week", "Текущий план"),
    ("shopping", "Список покупок"),
    ("help", "Что я умею"),
]

#: TZ-M7 §3.1 / А2: бот работает только в личных чатах
GROUP_REFUSAL = "Я работаю только в личных сообщениях: напишите мне в личку."
#: одна фраза на групповой чат в 10 минут, а не на каждое сообщение
GROUP_NOTICE_WINDOW_SECONDS = 600.0


class _DefaultMarkup:
    """Метка «поставить главное меню»: ``None`` значит «без клавиатуры»."""

    __slots__ = ()


#: значение по умолчанию для ``reply_markup``: подставить ``KEYBOARD``
MENU = _DefaultMarkup()


class TelegramApiError(httpx.HTTPError):
    """Ошибка Bot API с разобранным ``description`` (REPORT-2026-08-18 §4).

    Наследуемся от ``httpx.HTTPError`` сознательно: существующие
    ``except httpx.HTTPError`` в цикле опроса продолжают её ловить.
    """

    def __init__(self, method: str, status: int, description: str) -> None:
        self.method = method
        self.status = status
        self.description = description
        super().__init__(f"{method} → {status}: {description}")


def _describe(response: httpx.Response) -> str:
    """Причина отказа: ``description`` из тела, иначе обрезанный текст ответа."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        return str(body.get("description") or body)[:300]
    return str(body)[:300]


class TelegramClient:
    """Тонкая обёртка Bot API: getUpdates, sendMessage, edit, ack."""

    def __init__(self, token: str, http: httpx.AsyncClient) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.http = http

    async def _call(
        self,
        method: str,
        payload: dict,
        *,
        timeout: float = 30.0,
        raise_on_error: bool = True,
    ) -> dict | None:
        """Единственный канал общения с Bot API.

        При любом не-2xx пишет в лог причину из тела (``description``) — без
        этого ошибки Telegram были нечитаемы (REPORT-2026-08-18 §4).
        ``raise_on_error=False`` — для косметики (ack, typing), где падение
        обработки хуже, чем потерянный вызов.
        """
        try:
            response = await self.http.post(
                f"{self.base_url}/{method}", json=payload, timeout=timeout
            )
        except httpx.HTTPError:
            log.warning("Bot API %s не доставлен", method, exc_info=True)
            if raise_on_error:
                raise
            return None
        if response.status_code // 100 != 2:
            description = _describe(response)
            # «not modified» — двойной клик по той же кнопке, а не сбой
            level = logging.DEBUG if "not modified" in description else logging.WARNING
            log.log(level, "Bot API %s → %s: %s", method, response.status_code, description)
            if raise_on_error:
                raise TelegramApiError(method, response.status_code, description)
            return None
        body = response.json()
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    async def get_updates(self, offset: int | None) -> list[dict]:
        params: dict = {
            "timeout": POLL_TIMEOUT_SECONDS,
            "allowed_updates": '["message","callback_query"]',
        }
        if offset is not None:
            params["offset"] = offset
        response = await self.http.get(
            f"{self.base_url}/getUpdates", params=params,
            timeout=POLL_TIMEOUT_SECONDS + 10,
        )
        if response.status_code // 100 != 2:
            description = _describe(response)
            log.warning("Bot API getUpdates → %s: %s", response.status_code, description)
            raise TelegramApiError("getUpdates", response.status_code, description)
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"getUpdates: {payload}")
        return payload.get("result", [])

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None | _DefaultMarkup = MENU,
    ) -> int | None:
        """Отправка с разбиением; клавиатура — только на последнем куске.

        ``MENU`` (по умолчанию) — подставить постоянное меню, ``None`` — не
        ставить клавиатуру вовсе (так уходит плейсхолдер «⏳», TZ-M7 §4.5),
        словарь — поставить его. Возвращает message_id последнего сообщения.
        """
        chunks = split_for_telegram(text) or [""]
        message_id: int | None = None
        for index, chunk in enumerate(chunks):
            payload: dict = {"chat_id": chat_id, "text": chunk}
            if index == len(chunks) - 1:
                markup = KEYBOARD if isinstance(reply_markup, _DefaultMarkup) else reply_markup
                if markup is not None:
                    payload["reply_markup"] = markup
            result = await self._call("sendMessage", payload) or {}
            message_id = result.get("message_id")
        return message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ) -> int | None:
        """Правка сообщения; длинный текст режется, при отказе уходит новым.

        Возвращает message_id актуального сообщения (нового — если сработал
        запасной путь), чтобы «⏳» не оставался висеть навсегда
        (REPORT-2026-08-18 §4).
        """
        chunks = split_for_telegram(text) or [""]
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": chunks[0]}
        # клавиатуру вешаем на последний кусок: если есть хвост, она уедет с ним
        if reply_markup is not None and len(chunks) == 1:
            payload["reply_markup"] = reply_markup
        try:
            await self._call("editMessageText", payload)
        except TelegramApiError as error:
            if error.status == 400 and "not modified" in error.description:
                return message_id  # двойной клик по той же галке — не ошибка
            log.warning("Правка не удалась, отправляю новым сообщением: %s", error)
            return await self.send_message(chat_id, text, reply_markup)
        tail = chunks[1:]
        for index, chunk in enumerate(tail):
            tail_markup = reply_markup if index == len(tail) - 1 else None
            message_id = await self.send_message(chat_id, chunk, tail_markup) or message_id
        return message_id

    async def answer_callback_query(
        self, callback_query_id: str, text: str = "", show_alert: bool = False
    ) -> None:
        # ack не должен ронять обработку — спиннер погаснет по таймауту
        await self._call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text[:200],
                "show_alert": show_alert,
            },
            timeout=15,
            raise_on_error=False,
        )

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._call(
            "sendChatAction",
            {"chat_id": chat_id, "action": action},
            timeout=15,
            raise_on_error=False,
        )

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """Меню команд в личных чатах (TZ-M7 §4.1); сбой не мешает старту."""
        await self._call(
            "setMyCommands",
            {
                "commands": [
                    {"command": command, "description": description}
                    for command, description in commands
                ],
                "scope": {"type": "all_private_chats"},
            },
            timeout=15,
            raise_on_error=False,
        )


async def apply_callback_reply(
    client: TelegramClient, chat_id: int, message_id: int | None, result: CallbackReply
) -> None:
    if result.edit is not None and message_id is not None:
        await client.edit_message_text(
            chat_id, message_id, result.edit.text, result.edit.keyboard
        )
    for reply in result.sends:
        # None у Reply означает «инлайн-клавиатуры нет», а не «убрать меню»
        await client.send_message(
            chat_id, reply.text, reply.keyboard if reply.keyboard is not None else MENU
        )


class BotApp:
    """Состояние цикла: репозитории, фоновые задачи, защита от даблкликов."""

    def __init__(
        self,
        client: TelegramClient,
        bot_repository: BotRepository,
        app_repository: AppRepository,
        *,
        router: Router | None = None,
    ) -> None:
        self.client = client
        self.bot_repository = bot_repository
        self.app_repository = app_repository
        self.router = router or Router()
        self.tasks: set[asyncio.Task] = set()
        #: тот же объект, что у роутера: тесты и логика смотрят в одно место
        self.in_flight = self.router.in_flight
        #: тяжёлая операция не должна оставлять «⏳» навсегда (TZ-M7 §9.9);
        #: атрибут, а не константа, — тесты подставляют свой лимит
        self.heavy_timeout = HEAVY_TIMEOUT_SECONDS
        #: одна фраза на групповой чат за окно, а не ответ на каждое сообщение
        self.group_notices = RateLimiter(1, GROUP_NOTICE_WINDOW_SECONDS)

    def _today(self):
        return datetime.now(TIMEZONE).date()

    async def handle_text(self, actor: Actor, text: str) -> None:
        if not self.router.allow_text(actor):
            refusal = self.router.refusal_text(actor)
            if refusal is not None:
                await self.client.send_message(actor.chat_id, refusal, MENU)
            return
        try:
            reply = await handle_message(
                self.bot_repository, actor.user_id, text, self._today()
            )
        except Exception:
            log.exception("Ошибка обработки сообщения от %s", actor.user_id)
            reply = Reply("Что-то сломалось на моей стороне. Попробуйте ещё раз чуть позже.")
        await self.client.send_message(
            actor.chat_id, reply.text,
            reply.keyboard if reply.keyboard is not None else MENU,
        )

    async def refuse_group(self, chat_id: int) -> None:
        """Групповой чат: одна фраза за окно, остальное молча игнорируем."""
        if self.group_notices.hit(str(chat_id)) == 0:
            await self.client.send_message(chat_id, GROUP_REFUSAL, None)

    async def handle_callback_query(self, incoming: Incoming) -> None:
        actor, data = incoming.actor, incoming.data
        callback_id = incoming.callback_id
        if actor.chat_id == 0:
            # сообщение слишком старое — только погасить спиннер
            await self.client.answer_callback_query(callback_id)
            return
        if not self.router.allow_callback(actor):
            await self.client.answer_callback_query(callback_id, TOO_FAST_TEXT, True)
            return
        verb = callback_verb(data)

        if verb in HEAVY_VERBS:
            refusal = self.router.acquire_heavy(actor)
            if refusal is not None:
                await self.client.answer_callback_query(callback_id, refusal)
                return
            await self.client.answer_callback_query(
                callback_id, "Подбираю варианты…" if verb == "x" else "Меняю блюдо…"
            )
            await self.client.send_chat_action(actor.chat_id)
            # плейсхолдер: для замены редактируем сообщение с кнопками —
            # заодно исчезает клавиатура и даблклик невозможен физически
            placeholder_id = actor.message_id
            placeholder_text = "⏳ Ищу альтернативы…" if verb == "x" else "⏳ Применяю замену…"
            if verb == "x":
                placeholder_id = await self.client.send_message(
                    actor.chat_id, placeholder_text, None
                )
            elif actor.message_id is not None:
                await self.client.edit_message_text(
                    actor.chat_id, actor.message_id, placeholder_text
                )

            task = asyncio.create_task(self._run_heavy(actor, placeholder_id, data))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return

        # лёгкие глаголы — inline
        try:
            result = await handle_callback(
                self.app_repository, self.bot_repository, actor.user_id, data, self._today()
            )
        except Exception:
            log.exception("Ошибка callback %r от %s", data, actor.user_id)
            result = CallbackReply(toast="Что-то сломалось. Попробуйте ещё раз.")
        await self.client.answer_callback_query(callback_id, result.toast, result.show_alert)
        try:
            await apply_callback_reply(self.client, actor.chat_id, actor.message_id, result)
        except httpx.HTTPError:
            log.exception("Не удалось доставить ответ на callback в чат %s", actor.chat_id)

    async def _run_heavy(
        self, actor: Actor, placeholder_id: int | None, data: str
    ) -> None:
        chat_id = actor.chat_id
        try:
            result = await asyncio.wait_for(
                handle_callback(
                    self.app_repository, self.bot_repository, actor.user_id, data,
                    self._today(),
                ),
                self.heavy_timeout,
            )
            await apply_callback_reply(self.client, chat_id, placeholder_id, result)
            if result.edit is None and placeholder_id is not None:
                await self.client.edit_message_text(
                    chat_id, placeholder_id, result.toast or "Готово."
                )
        except TimeoutError:
            log.warning("Тяжёлая операция %r от %s не уложилась в срок", data, actor.user_id)
            if placeholder_id is not None:
                try:
                    await self.client.edit_message_text(
                        chat_id, placeholder_id,
                        "Не успел за отведённое время. Попробуйте ещё раз.",
                    )
                except httpx.HTTPError:
                    pass
        except Exception:
            log.exception("Ошибка фоновой задачи %r от %s", data, actor.user_id)
            if placeholder_id is not None:
                try:
                    await self.client.edit_message_text(
                        chat_id, placeholder_id, "Не получилось, попробуйте ещё раз."
                    )
                except httpx.HTTPError:
                    pass
        finally:
            self.router.release_heavy(actor)

    async def process_updates(self, updates: list[dict]) -> int | None:
        next_offset: int | None = None
        for update in updates:
            next_offset = int(update["update_id"]) + 1
            incoming = parse_update(update)
            if incoming is None:
                continue
            if incoming.kind == "group":
                # TZ-M7 §3.1 / А2: в группе не работаем
                if incoming.callback_id:
                    await self.client.answer_callback_query(
                        incoming.callback_id, GROUP_REFUSAL, True
                    )
                else:
                    await self.refuse_group(incoming.actor.chat_id)
                continue
            if incoming.kind == "callback":
                await self.handle_callback_query(incoming)
                continue
            await self.handle_text(incoming.actor, incoming.text)
        return next_offset


#: объекты TZ-M7, без которых бот не работает; DDL накатывает web
SCHEMA_READY_SQL = """
    SELECT to_regclass('app_core.telegram_dialog_state') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema='app_core' AND table_name='users'
             AND column_name='active_household_id'
       )
"""


async def wait_for_schema(pool, *, attempts: int = 60, delay: float = 5.0) -> None:
    """Дождаться, пока web накатит DDL (AppRepository.connect).

    Бот сознательно не мигрирует сам: одновременный идемпотентный DDL из двух
    процессов — лишний источник гонок. Ждём и говорим об этом внятно.
    """
    for attempt in range(1, attempts + 1):
        if await pool.fetchval(SCHEMA_READY_SQL):
            return
        log.warning(
            "В схеме БД нет объектов TZ-M7 (попытка %s из %s). Перезапустите web.",
            attempt, attempts,
        )
        await asyncio.sleep(delay)
    raise SystemExit("Схема БД устарела: web не накатил DDL TZ-M7. Перезапустите web.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан — боту нечем авторизоваться.")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL не задан.")

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    await wait_for_schema(pool)
    bot_repository = BotRepository(pool)
    # Разделяем пул с AppRepository: схему БД накатывает веб-приложение,
    # боту второй пул и повторный прогон schema.sql не нужны.
    # channel='telegram' — чтобы audit_log не помечал действия бота как 'web'.
    app_repository = AppRepository(database_url, channel="telegram")
    app_repository.pool = pool

    offset: int | None = None
    async with httpx.AsyncClient() as http:
        client = TelegramClient(token, http)
        await client.set_my_commands(BOT_COMMANDS)
        app = BotApp(client, bot_repository, app_repository)
        log.info("Бот запущен, ожидаю сообщения (long polling).")
        while True:
            try:
                updates = await client.get_updates(offset)
                new_offset = await app.process_updates(updates)
                if new_offset is not None:
                    offset = new_offset
            except (httpx.HTTPError, RuntimeError, asyncpg.PostgresError):
                log.exception("Сбой цикла опроса, пауза %s с", ERROR_BACKOFF_SECONDS)
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
