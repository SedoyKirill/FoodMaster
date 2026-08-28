"""Сцены бота: многошаговые диалоги поверх ``fsm.DialogStore`` (TZ-M7 §2).

Сцена — чистая функция от контекста: получает, что ввёл человек и что уже
собрано, возвращает ``Reply``. Ни сети, ни знания о транспорте.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..fsm import DialogState, DialogStore
from ..render import Reply
from ..repository import BotRepository
from ..router import Actor


@dataclass
class SceneContext:
    """Всё, что нужно шагу сцены, — и ничего сверх того."""

    actor: Actor
    text: str
    state: DialogState
    bot_repository: BotRepository
    app_repository: Any
    dialogs: DialogStore
    today: date
    #: псевдосессия для AppRepository; None — сцена работает до входа в аккаунт
    session: dict[str, Any] | None = None


__all__ = ["SceneContext", "Reply", "DialogState"]
