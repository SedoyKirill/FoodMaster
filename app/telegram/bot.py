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

from .service import (
    BotRepository, CallbackReply, Reply, callback_verb, handle_callback,
    handle_message, split_for_telegram,
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


class TelegramClient:
    """Тонкая обёртка Bot API: getUpdates, sendMessage, edit, ack."""

    def __init__(self, token: str, http: httpx.AsyncClient) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.http = http

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
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"getUpdates: {payload}")
        return payload.get("result", [])

    async def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None = None
    ) -> int | None:
        """Отправка с разбиением; клавиатура — только на последнем куске.

        Возвращает message_id последнего отправленного сообщения.
        """
        chunks = split_for_telegram(text) or [""]
        message_id: int | None = None
        for index, chunk in enumerate(chunks):
            payload: dict = {"chat_id": chat_id, "text": chunk}
            if index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup if reply_markup is not None else KEYBOARD
            response = await self.http.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=30
            )
            response.raise_for_status()
            body = response.json()
            message_id = (body.get("result") or {}).get("message_id")
        return message_id

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None
    ) -> None:
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await self.http.post(
            f"{self.base_url}/editMessageText", json=payload, timeout=30
        )
        if response.status_code == 400 and "not modified" in response.text:
            return  # двойной клик по той же галке — не ошибка
        response.raise_for_status()

    async def answer_callback_query(
        self, callback_query_id: str, text: str = "", show_alert: bool = False
    ) -> None:
        try:
            await self.http.post(
                f"{self.base_url}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    "text": text[:200],
                    "show_alert": show_alert,
                },
                timeout=15,
            )
        except httpx.HTTPError:
            # ack не должен ронять обработку — спиннер погаснет по таймауту
            log.warning("answerCallbackQuery не доставлен", exc_info=True)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self.http.post(
                f"{self.base_url}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
                timeout=15,
            )
        except httpx.HTTPError:
            pass  # косметика


async def apply_callback_reply(
    client: TelegramClient, chat_id: int, message_id: int | None, result: CallbackReply
) -> None:
    if result.edit is not None and message_id is not None:
        await client.edit_message_text(
            chat_id, message_id, result.edit.text, result.edit.keyboard
        )
    for reply in result.sends:
        await client.send_message(chat_id, reply.text, reply.keyboard)


class BotApp:
    """Состояние цикла: репозитории, фоновые задачи, защита от даблкликов."""

    def __init__(
        self,
        client: TelegramClient,
        bot_repository: BotRepository,
        app_repository: AppRepository,
    ) -> None:
        self.client = client
        self.bot_repository = bot_repository
        self.app_repository = app_repository
        self.tasks: set[asyncio.Task] = set()
        self.in_flight: set[tuple[int, str]] = set()

    def _today(self):
        return datetime.now(TIMEZONE).date()

    async def handle_text(self, chat_id: int, text: str) -> None:
        try:
            reply = await handle_message(self.bot_repository, chat_id, text, self._today())
        except Exception:
            log.exception("Ошибка обработки сообщения из чата %s", chat_id)
            reply = Reply("Что-то сломалось на моей стороне. Попробуйте ещё раз чуть позже.")
        await self.client.send_message(chat_id, reply.text, reply.keyboard)

    async def handle_callback_query(self, callback: dict) -> None:
        callback_id = str(callback.get("id"))
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if chat_id is None:
            # сообщение слишком старое — только погасить спиннер
            await self.client.answer_callback_query(callback_id)
            return
        chat_id = int(chat_id)
        verb = callback_verb(data)

        if verb in HEAVY_VERBS:
            flight_key = (chat_id, data)
            if flight_key in self.in_flight:
                await self.client.answer_callback_query(callback_id, "Уже работаю, секунду…")
                return
            self.in_flight.add(flight_key)
            await self.client.answer_callback_query(
                callback_id, "Подбираю варианты…" if verb == "x" else "Меняю блюдо…"
            )
            await self.client.send_chat_action(chat_id)
            # плейсхолдер: для замены редактируем сообщение с кнопками —
            # заодно исчезает клавиатура и даблклик невозможен физически
            placeholder_id = message_id
            placeholder_text = "⏳ Ищу альтернативы…" if verb == "x" else "⏳ Применяю замену…"
            if verb == "x":
                placeholder_id = await self.client.send_message(chat_id, placeholder_text, None)
            elif message_id is not None:
                await self.client.edit_message_text(chat_id, message_id, placeholder_text)

            task = asyncio.create_task(
                self._run_heavy(chat_id, placeholder_id, data, flight_key)
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return

        # лёгкие глаголы — inline
        try:
            result = await handle_callback(
                self.app_repository, self.bot_repository, chat_id, data, self._today()
            )
        except Exception:
            log.exception("Ошибка callback %r из чата %s", data, chat_id)
            result = CallbackReply(toast="Что-то сломалось. Попробуйте ещё раз.")
        await self.client.answer_callback_query(callback_id, result.toast, result.show_alert)
        try:
            await apply_callback_reply(self.client, chat_id, message_id, result)
        except httpx.HTTPError:
            log.exception("Не удалось доставить ответ на callback в чат %s", chat_id)

    async def _run_heavy(
        self, chat_id: int, placeholder_id: int | None, data: str, flight_key: tuple
    ) -> None:
        try:
            result = await handle_callback(
                self.app_repository, self.bot_repository, chat_id, data, self._today()
            )
            await apply_callback_reply(self.client, chat_id, placeholder_id, result)
            if result.edit is None and placeholder_id is not None:
                await self.client.edit_message_text(
                    chat_id, placeholder_id, result.toast or "Готово."
                )
        except Exception:
            log.exception("Ошибка фоновой задачи %r из чата %s", data, chat_id)
            if placeholder_id is not None:
                try:
                    await self.client.edit_message_text(
                        chat_id, placeholder_id, "Не получилось, попробуйте ещё раз."
                    )
                except httpx.HTTPError:
                    pass
        finally:
            self.in_flight.discard(flight_key)

    async def process_updates(self, updates: list[dict]) -> int | None:
        next_offset: int | None = None
        for update in updates:
            next_offset = int(update["update_id"]) + 1
            if "callback_query" in update:
                await self.handle_callback_query(update["callback_query"])
                continue
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = message.get("text")
            if chat_id is None or not text:
                continue
            await self.handle_text(int(chat_id), str(text))
        return next_offset


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан — боту нечем авторизоваться.")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL не задан.")

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)
    bot_repository = BotRepository(pool)
    # Разделяем пул с AppRepository: схему БД накатывает веб-приложение,
    # боту второй пул и повторный прогон schema.sql не нужны.
    app_repository = AppRepository(database_url)
    app_repository.pool = pool

    offset: int | None = None
    async with httpx.AsyncClient() as http:
        client = TelegramClient(token, http)
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
