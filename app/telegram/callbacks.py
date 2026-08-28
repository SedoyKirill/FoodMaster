"""Кодек ``callback_data``: ``<verb>:<args>`` в 64 байтах (TZ-M7 §4.3).

Telegram даёт на кнопку 64 байта — это весь бюджет на «что делать» и «с чем».
Поэтому UUID пакуются в 22 символа base64url, а глагол занимает один символ.
"""

from __future__ import annotations

import base64
import uuid as uuid_mod
from typing import Any

#: жёсткий лимит Bot API на callback_data, байты
CALLBACK_LIMIT = 64
#: разделитель по ТЗ §4.3
SEPARATOR = ":"
#: формат до T3 — у людей в чатах остались кнопки со старым разделителем
LEGACY_SEPARATOR = "|"

#: Глаголы из таблицы §4.3. Один символ на глагол — бюджет в 64 байта тесный.
VERBS: dict[str, str] = {
    "s": "тумблер «куплено»",
    "r": "карточка рецепта",
    "x": "подобрать замену",
    "v": "применить замену",
    "c": "оставить как есть",
    "d": "показать день плана",
    "p": "страница списка",
    "k": "отметка приготовили/пропустили",
    "g": "оценка рецепта",
    "w": "статус проверки рецепта",
    "i": "запасы: удалить",
    "f": "фильтр списка",
    "o": "переключатель в настройках",
    "n": "навигация по сценам",
    "t": "онбординг вкуса",
    "y": "подтверждение опасного действия",
}


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
    """``<verb>:<args>``. Превышение 64 байт — ошибка проектирования кнопки,
    а не пользователя: раньше это был ``assert``, который исчезал под -O."""
    encoded = SEPARATOR.join([verb, *(str(part) for part in parts)])
    if len(encoded.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError(f"callback_data длиннее {CALLBACK_LIMIT} байт: {encoded!r}")
    return encoded


def parse_callback(data: str) -> tuple[str, list[str]] | None:
    """Разбор кнопки; None — мусор или неизвестный глагол.

    Понимает и старый разделитель «|»: кнопки, отправленные до перехода на
    «:», остаются в чатах пользователей и должны продолжать работать.
    Алфавит base64url (A-Za-z0-9-_) не пересекается ни с одним из них.
    """
    if not data:
        return None
    separator = SEPARATOR if SEPARATOR in data else LEGACY_SEPARATOR
    verb, *parts = data.split(separator)
    if verb not in VERBS:
        return None
    return verb, parts


def callback_verb(data: str) -> str:
    parsed = parse_callback(data or "")
    return parsed[0] if parsed else ""
