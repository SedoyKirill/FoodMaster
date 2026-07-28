"""OpenAPI tags, one per API area.

Fixed in M1 so that the tag names — and therefore the generated TypeScript
symbol names in M6 — are stable from the first endpoint onwards.
"""

from __future__ import annotations

from typing import Any

OPENAPI_TAGS: list[dict[str, Any]] = [
    {"name": "system", "description": "Состояние системы и диагностика (M1/M6)."},
    {"name": "family", "description": "Семья, профили её членов, дневник веса (M4/M7)."},
    {"name": "nutrition", "description": "Детерминированная математика питания (M4)."},
    {"name": "recipes", "description": "Поиск рецептов и карточка рецепта (M2)."},
    {"name": "stores", "description": "Каталоги магазинов, предложения, история цен (M3)."},
    {"name": "plans", "description": "Генерация и жизненный цикл рациона (M5)."},
    {"name": "shopping", "description": "Список покупок активного плана (M5)."},
    {"name": "fridge", "description": "Виртуальный холодильник (M5)."},
    {"name": "diary", "description": "Дневник питания (M7)."},
    {"name": "expenses", "description": "Траты и месячный бюджет (M7)."},
    {"name": "analytics", "description": "Отчёты и графики (M7)."},
    {"name": "admin", "description": "Очередь матчинга, журнал прогонов, обслуживание (M3/M6)."},
]
