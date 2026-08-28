"""Ограничение частоты запросов в памяти процесса (TZ-M6R A6 / AUDIT S2).

Внешних зависимостей не добавляем: приложение локальное и работает одним
воркером uvicorn, поэтому состояния в памяти достаточно. При переходе на
несколько воркеров счётчик станет пер-процессным — это отмечено в README.
"""

from __future__ import annotations

import math
import time
from typing import Callable


class RateLimiter:
    """Токен-бакет: ``capacity`` попыток за ``window_seconds``, плавное пополнение.

    Часы инъектируются, чтобы тесты не спали (TZ-TESTS §2.5).
    """

    def __init__(
        self,
        capacity: int = 5,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity должен быть не меньше 1")
        self.capacity = float(capacity)
        self.window = float(window_seconds)
        self.rate = self.capacity / self.window
        self.clock = clock
        self.max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}

    def hit(self, key: str) -> int:
        """0 — запрос разрешён; иначе Retry-After в целых секундах."""
        now = self.clock()
        tokens, updated = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - updated) * self.rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return max(1, math.ceil((1.0 - tokens) / self.rate))
        self._buckets[key] = (tokens - 1.0, now)
        if len(self._buckets) > self.max_keys:
            self._prune(now)
        return 0

    def reset(self, key: str) -> None:
        """Снять ограничение с ключа (например, после успешного входа)."""
        self._buckets.pop(key, None)

    def _prune(self, now: float) -> None:
        self._buckets = {
            key: value for key, value in self._buckets.items()
            if now - value[1] < self.window * 2
        }
