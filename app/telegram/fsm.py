"""Состояние диалога: многошаговые формы бота (TZ-M7 §4.2).

Telegram не помнит, о чём шёл разговор, — помним мы. Одна строка на человека
в ``app_core.telegram_dialog_state``: какая сцена, какой шаг и что уже введено.

Два правила, из которых растёт всё остальное:

* в ``data`` живут **только поля текущей формы** — тексты сообщений не храним
  (TZ-M1, TZ-M7 §3.5);
* через 30 минут молчания форма забывается: человек, вернувшийся назавтра, не
  должен обнаружить, что его «привет» ушло в поле «бюджет» (приёмка §9.7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .callbacks import encode_callback

#: §4.2 — сколько живёт незавершённая форма
DIALOG_TTL = timedelta(minutes=30)

#: «✖ Отмена» есть на каждом шаге любой сцены
CANCEL_DATA = encode_callback("n", "cancel")
CANCEL_BUTTON = {"text": "✖ Отмена", "callback_data": CANCEL_DATA}
CANCEL_TEXT = "Отменил. Что дальше?"
#: /cancel и подпись кнопки — одно и то же действие
CANCEL_COMMANDS = frozenset({"/cancel", "отмена", "✖ отмена"})


@dataclass
class DialogState:
    """Незавершённая форма: сцена, шаг и уже собранные поля."""

    scene: str
    step: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


def is_cancel(text: str) -> bool:
    """Отмена ли это — чистая функция, без обращений к базе."""
    return (text or "").strip().lower() in CANCEL_COMMANDS


def is_expired(
    updated_at: datetime | None, now: datetime, ttl: timedelta = DIALOG_TTL
) -> bool:
    """Протухла ли форма. Без ``updated_at`` считаем протухшей: строка в БД
    без отметки времени — след ручной правки, доверять ей нечего."""
    if updated_at is None:
        return True
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return now - updated_at > ttl


class DialogStore:
    """Чтение и запись состояния диалога.

    Часы инъектируются (TZ-TESTS §2.5), чтобы тесты протухания не спали
    по полчаса.
    """

    def __init__(
        self,
        pool: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = DIALOG_TTL,
    ) -> None:
        self.pool = pool
        self.clock = clock
        self.ttl = ttl

    async def load(self, user_id: int) -> DialogState | None:
        """Активная форма или None. Протухшую подчищает сразу."""
        row = await self.pool.fetchrow(
            """
            SELECT scene, step, data, updated_at
            FROM app_core.telegram_dialog_state WHERE user_id=$1
            """,
            int(user_id),
        )
        if not row:
            return None
        row = dict(row)
        if is_expired(row.get("updated_at"), self.clock(), self.ttl):
            await self.clear(user_id)
            return None
        return DialogState(
            scene=str(row["scene"]),
            step=str(row.get("step") or ""),
            data=_as_dict(row.get("data")),
            updated_at=row.get("updated_at"),
        )

    async def save(self, user_id: int, state: DialogState) -> None:
        now = self.clock()
        await self.pool.execute(
            """
            INSERT INTO app_core.telegram_dialog_state (user_id, scene, step, data, updated_at)
            VALUES ($1,$2,$3,$4::jsonb,$5)
            ON CONFLICT (user_id) DO UPDATE
            SET scene=EXCLUDED.scene, step=EXCLUDED.step,
                data=EXCLUDED.data, updated_at=EXCLUDED.updated_at
            """,
            int(user_id), state.scene, state.step,
            json.dumps(state.data or {}, ensure_ascii=False), now,
        )
        state.updated_at = now

    async def clear(self, user_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM app_core.telegram_dialog_state WHERE user_id=$1", int(user_id)
        )


def _as_dict(value: Any) -> dict[str, Any]:
    """JSONB без кодека приходит строкой — тот же приём, что в database.py."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
