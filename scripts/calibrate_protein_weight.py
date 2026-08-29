"""Подбор веса белка в целевой функции (TZ-M8 §6.4, §9.2).

Пока справочник КБЖУ покрывал шестую часть ингредиентов, поднимать
``weights.protein`` было бессмысленно: солвер оптимизировал бы то, о чём нет
данных. После волны разметки (29.08.2026, покрытие 99 % по упоминаниям) вес
можно подбирать честно.

Скрипт собирает один и тот же план с разными значениями веса и печатает, что
получается: доля дней с недобором белка, стоимость тысячи калорий и сколько
белка вышло от цели. Ничего не записывает.

Запуск:
    DATABASE_URL=postgresql://ration:ration@127.0.0.1:5432/ration \\
        python scripts/calibrate_protein_weight.py --mode fitness --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.web.database import AppRepository  # noqa: E402
from app.web.planner import build_plan  # noqa: E402
from app.web.planning import profile as profile_mod  # noqa: E402
from app.web.planning import weights as weights_mod  # noqa: E402

#: значения веса для перебора; 150 — стартовое из ТЗ для режима «фитнес»
DEFAULT_SWEEP = (0, 150, 400, 800, 1500, 3000)


def day_protein(plan: dict[str, Any], people: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """(факт, цель) по дням, где белок известен у всех блюд дня."""
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for meal in plan["meals"]:
        by_day.setdefault(meal["meal_date"], []).append(meal)
    result = []
    for day, meals in sorted(by_day.items()):
        if any(meal.get("estimated_protein") is None for meal in meals):
            continue
        target = sum(
            profile_mod.slot_protein_target(people, meal["meal_type"], day) for meal in meals
        )
        if not target:
            continue
        result.append((sum(int(meal["estimated_protein"]) for meal in meals), target))
    return result


def kcal_of(plan: dict[str, Any]) -> int:
    return sum(int(meal["estimated_kcal"] or 0) for meal in plan["meals"])


async def main() -> None:
    parser = argparse.ArgumentParser(description="Подбор веса белка (TZ-M8 §9.2)")
    parser.add_argument("--mode", default="fitness")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--households", type=int, default=2)
    parser.add_argument(
        "--weights",
        default=",".join(str(value) for value in DEFAULT_SWEEP),
        help="значения веса через запятую",
    )
    args = parser.parse_args()
    sweep = [int(value) for value in args.weights.split(",") if value.strip()]

    repository = AppRepository()
    await repository.connect()
    try:
        households = await repository.db().fetch(
            """
            SELECT h.id, h.name FROM app_core.households h
            JOIN app_core.people p ON p.household_id = h.id
            GROUP BY h.id, h.name ORDER BY h.created_at LIMIT $1
            """,
            args.households,
        )
        starts_on = date.today() + timedelta(days=1)
        print(f"\nРежим «{args.mode}», горизонт {args.days} дней\n")
        print("| вес | семья | дней с недобором | белок к цели | ₽/1000 ккал |")
        print("|---|---|---|---|---|")
        for weight in sweep:
            profile_weights = replace(weights_mod.weights_for(args.mode), protein=weight)
            for household in households:
                session = {"household_id": household["id"], "role": "owner"}
                plan_profile = await repository.plan_profile(session)
                data = await repository.planner_data(
                    session, list(plan_profile["cuisines"]), starts_on
                )
                plan = build_plan(
                    household_id=str(household["id"]),
                    starts_on=starts_on,
                    days=args.days,
                    cuisines=list(plan_profile["cuisines"]),
                    cuisine_mode=str(plan_profile["cuisine_mode"]),
                    mode=args.mode,
                    weights=profile_weights,
                    meals=list(plan_profile["meals"]),
                    **data,
                )
                days = day_protein(plan, data["people"])
                if not days:
                    print(f"| {weight} | {household['name']} | нет данных | — | — |")
                    continue
                short = sum(1 for actual, target in days if actual < target * 0.8)
                ratio = statistics.fmean(actual / target for actual, target in days)
                kcal = kcal_of(plan)
                cost = plan["estimated_cost_kop"] / 100 / (kcal / 1000) if kcal else 0
                print(
                    f"| {weight} | {household['name'][:16]} | {short}/{len(days)} | "
                    f"{ratio * 100:.0f} % | {cost:.0f} ₽ |"
                )
    finally:
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
