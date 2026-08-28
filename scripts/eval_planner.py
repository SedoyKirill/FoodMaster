"""Офлайн-оценка качества планировщика (TZ-M8 §9.2).

Запуск на живой базе:

    docker compose --profile manual run --rm -T recipe-importer \
        python scripts/eval_planner.py

или локально, если Postgres проброшен наружу:

    DATABASE_URL=postgresql://ration:ration@127.0.0.1:5432/ration \
        python scripts/eval_planner.py --days 7

Скрипт ничего не записывает: он собирает планы в памяти и печатает таблицу
метрик. Веса калибруются по ней вручную — единственное место весов остаётся
``app/web/planning/weights.py``.

Метрики §9.2:

* **novelty@N** — доля блюд плана, которых не было в последние три недели.
  Цель ≥ 60 % при ``novelty = medium``;
* **taste@N** — средний аффинити выбранных блюд минус средний аффинити
  случайного допустимого набора. Цель ≥ +0.2: иначе модель вкуса не отличает
  любимое от произвольного;
* **cost/1000 ккал** — сколько стоит тысяча калорий плана. Сравниваются
  режимы: ``economy`` должен быть дешевле ``balanced`` минимум на 10 %;
* **protein_gap** — доля дней, где белка меньше нормы более чем на 20 %.
  В режиме ``fitness`` цель ≤ 10 %;
* **stability** — тот же вход даёт тот же план (иначе пересборка меню
  выглядит случайным подбором).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
from datetime import date, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.web.database import AppRepository  # noqa: E402
from app.web.planner import build_plan  # noqa: E402
from app.web.planning import profile as profile_mod  # noqa: E402
from app.web.planning import taste as taste_mod  # noqa: E402

#: сколько случайных наборов усредняется в базовой линии taste@N
BASELINE_SAMPLES = 20
#: недобор белка за день, начиная с которого день считается провальным
PROTEIN_GAP_SHARE = 0.2
#: цели приёмки §9.2 — печатаются рядом с фактом, чтобы отчёт читался сам
TARGETS = {
    "novelty": "≥ 60 % (medium)",
    "taste_delta": "≥ +0.20",
    "protein_gap": "≤ 10 % (fitness)",
    "stability": "1 план",
}


def _plan_recipe_ids(plan: dict[str, Any]) -> list[int]:
    return [int(meal["recipe_id"]) for meal in plan["meals"]]


def novelty(plan: dict[str, Any], history: list[dict[str, Any]]) -> float:
    """Доля блюд, которых не было в истории семьи за окно ротации."""
    seen = {int(row["recipe_id"]) for row in history}
    ids = _plan_recipe_ids(plan)
    if not ids:
        return 0.0
    return sum(1 for recipe_id in ids if recipe_id not in seen) / len(ids)


def taste_delta(
    plan: dict[str, Any], data: dict[str, Any], starts_on: date, seed: int
) -> float | None:
    """Насколько выбранные блюда вкуснее случайного допустимого набора.

    Без событий вкуса разницы быть не может — возвращается None, а не ноль:
    «модель не отличает» и «модель не обучалась» — разные утверждения.
    """
    events = data.get("taste_events") or []
    if not events:
        return None
    pool = data["recipes"]
    metas = taste_mod.build_metas(pool)
    model = taste_mod.TasteModel.fit(events, metas, starts_on)
    people = data["people"]

    def _affinity(recipe_id: int) -> float:
        meta = metas.get(recipe_id)
        return model.family_affinity(meta, people) if meta else 0.0

    chosen = [_affinity(recipe_id) for recipe_id in _plan_recipe_ids(plan)]
    if not chosen:
        return None
    ids = [int(recipe["id"]) for recipe in pool]
    rng = random.Random(seed)
    baseline = [
        statistics.fmean(_affinity(recipe_id) for recipe_id in rng.sample(ids, len(chosen)))
        for _ in range(BASELINE_SAMPLES)
        if len(ids) >= len(chosen)
    ]
    if not baseline:
        return None
    return statistics.fmean(chosen) - statistics.fmean(baseline)


def cost_per_1000_kcal(plan: dict[str, Any]) -> float | None:
    kcal = sum(
        int(meal["estimated_kcal"]) for meal in plan["meals"] if meal.get("estimated_kcal")
    )
    if not kcal:
        return None
    return plan["estimated_cost_kop"] / 100 / (kcal / 1000)


def protein_gap(plan: dict[str, Any], people: list[dict[str, Any]]) -> float | None:
    """Доля дней, где белка меньше нормы больше чем на пятую часть.

    День учитывается, только если белок известен у **всех** его блюд. Иначе
    метрика меряет не питание, а полноту разметки КБЖУ: цель считается по
    трём приёмам, а факт — по одному, и любой день выглядит провальным.
    """
    by_day: dict[Any, list[dict[str, Any]]] = {}
    for meal in plan["meals"]:
        by_day.setdefault(meal["meal_date"], []).append(meal)
    days = 0
    failed = 0
    for day, meals in by_day.items():
        if any(meal.get("estimated_protein") is None for meal in meals):
            continue
        target = sum(
            profile_mod.slot_protein_target(people, meal["meal_type"], day) for meal in meals
        )
        if not target:
            continue
        days += 1
        actual = sum(int(meal["estimated_protein"]) for meal in meals)
        if actual < target * (1 - PROTEIN_GAP_SHARE):
            failed += 1
    return failed / days if days else None


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %"


def _signed(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}"


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} ₽"


async def evaluate_household(
    repository: AppRepository,
    household_id: Any,
    name: str,
    days: int,
    modes: list[str],
    starts_on: date,
) -> list[dict[str, Any]]:
    session = {"household_id": household_id, "role": "owner"}
    profile = await repository.plan_profile(session)
    data = await repository.planner_data(session, list(profile["cuisines"]), starts_on)
    if not data["people"]:
        return []
    rows = []
    for mode in modes:
        plan = build_plan(
            household_id=str(household_id),
            starts_on=starts_on,
            days=days,
            cuisines=list(profile["cuisines"]),
            cuisine_mode=str(profile["cuisine_mode"]),
            mode=mode,
            meals=list(profile["meals"]),
            **data,
        )
        repeat = build_plan(
            household_id=str(household_id),
            starts_on=starts_on,
            days=days,
            cuisines=list(profile["cuisines"]),
            cuisine_mode=str(profile["cuisine_mode"]),
            mode=mode,
            meals=list(profile["meals"]),
            **data,
        )
        rows.append(
            {
                "household": name,
                "mode": mode,
                "meals": len(plan["meals"]),
                "novelty": novelty(plan, data["history"]),
                "taste_delta": taste_delta(plan, data, starts_on, seed=hash(mode) & 0xFFFF),
                "cost_per_1000": cost_per_1000_kcal(plan),
                "protein_gap": protein_gap(plan, data["people"]),
                "stable": _plan_recipe_ids(plan) == _plan_recipe_ids(repeat),
                "warnings": len(plan.get("warnings") or []),
            }
        )
    return rows


def print_report(rows: list[dict[str, Any]], days: int) -> None:
    print(f"\n## Офлайн-оценка планировщика, горизонт {days} дней\n")
    print("| Семья | Режим | Блюд | novelty@N | taste@N | ₽/1000 ккал | protein_gap | стабильно |")
    print("|---|---|---|---|---|---|---|---|")
    for row in rows:
        print(
            f"| {row['household']} | {row['mode']} | {row['meals']} | "
            f"{_percent(row['novelty'])} | {_signed(row['taste_delta'])} | "
            f"{_money(row['cost_per_1000'])} | {_percent(row['protein_gap'])} | "
            f"{'да' if row['stable'] else 'НЕТ'} |"
        )
    print("\n### Цели приёмки\n")
    for metric, target in TARGETS.items():
        print(f"- `{metric}` — {target}")

    economy = [row["cost_per_1000"] for row in rows if row["mode"] == "economy" and row["cost_per_1000"]]
    balanced = [row["cost_per_1000"] for row in rows if row["mode"] == "balanced" and row["cost_per_1000"]]
    if economy and balanced:
        saving = 1 - statistics.fmean(economy) / statistics.fmean(balanced)
        print(
            f"\n«Экономно» дешевле «сбалансированно» на {saving * 100:.0f} % "
            f"(цель — не меньше 10 %)."
        )
    unstable = [row for row in rows if not row["stable"]]
    if unstable:
        print(f"\n**Нестабильных планов: {len(unstable)}** — это дефект, а не разброс.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Офлайн-оценка планировщика (TZ-M8 §9.2)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--households", type=int, default=5, help="сколько семей взять")
    parser.add_argument(
        "--modes",
        default="balanced,economy,variety,fitness,quick",
        help="режимы через запятую",
    )
    parser.add_argument(
        "--starts-on", default=None, help="дата начала плана, по умолчанию завтра"
    )
    args = parser.parse_args()

    starts_on = (
        date.fromisoformat(args.starts_on)
        if args.starts_on
        else date.today() + timedelta(days=1)
    )
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]

    repository = AppRepository()
    await repository.connect()
    try:
        households = await repository.db().fetch(
            """
            SELECT h.id, h.name, count(p.id) AS people
            FROM app_core.households h
            LEFT JOIN app_core.people p ON p.household_id = h.id
            GROUP BY h.id, h.name
            HAVING count(p.id) > 0
            ORDER BY h.created_at
            LIMIT $1
            """,
            args.households,
        )
        if not households:
            print("Семей с людьми в базе нет — оценивать нечего.")
            return
        rows: list[dict[str, Any]] = []
        for household in households:
            # Имена семей не уникальны — в таблице без хвоста id две «Тест
            # M6R» выглядят одной строкой, продублированной по ошибке.
            label = f"{household['name']} · {str(household['id'])[:8]}"
            print(f"… считаем «{label}»", file=sys.stderr)
            rows.extend(
                await evaluate_household(
                    repository, household["id"], label, args.days, modes, starts_on
                )
            )
        print_report(rows, args.days)
    finally:
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
