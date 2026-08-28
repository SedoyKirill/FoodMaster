"""Оптимизация меню по горизонту: CP-SAT (OR-Tools) + жадный fallback.

TZ-M5R §2.3. Все веса — именованные константы в одном месте. Модель
минимизирует стоимость/штрафы; пустой слот допустим, но наказан так, что
выбирается только при реальной нехватке кандидатов (и честно попадает в
warnings, а не молча повторяет блюдо — дефект P2 старого планировщика).
"""

from __future__ import annotations

import hashlib
from typing import Any

from .candidates import CandidateScore

# Веса целевой функции (×10, чтобы дроби вроде 0.3·W_COST оставались целыми).
# Калибровка к стоимости: 100 условных единиц ≈ 10 ₽ при «Сбалансированно» —
# иначе цена задавила бы все вкусовые сигналы и меню состояло бы из самых
# дешёвых блюд библиотеки (проверено на живых данных: хлеб на все обеды).
W_COST = 10
W_BUDGET = 500
W_KCAL = 50
W_WASTE = 300
W_VARIETY = 400
W_TASTE = 500
W_CUISINE = 200
W_FIT = 300
# TZ-M8 §4: вкус семьи заменил звёзды в целевой функции. rating_bonus из
# данных не удалён, но в коэффициент больше не входит — оценка теперь одно из
# событий вкуса наравне с заменами и «приготовили».
W_TASTE_AFFINITY = 600
# Блюдо, о котором семья ничего не знает, слегка проигрывает знакомому: без
# этого меню новичка состояло бы из случайных находок каталога.
W_UNKNOWN = 150
# TZ-M8 §3.7: блюдо, которое ели вчера, должно проигрывать такому же, которого
# не было три недели. Вес сопоставим с рейтингом: ротация важна, но не важнее
# того, что семья действительно любит.
W_RECENCY = 400
# Сезонность — приятный, но не решающий сигнал (§3.6).
W_SEASON = 100
# Пустой слот хуже любого реалистичного перерасхода бюджета: план с дырами —
# крайняя мера при настоящей нехватке рецептов, а не способ «сэкономить».
W_EMPTY_SLOT = 10_000_000

#: множитель к W_COST по ценовой стратегии (×10: 3 / 1 / 0.3)
PRICE_TIER_COST_FACTOR = {"economy": 30, "balanced": 10, "premium": 3}

SOLVER_TIME_LIMIT_SECONDS = 10.0
SOLVER_WORKERS = 4
MAX_CANDIDATES_PER_SLOT = 40
MAX_USES_PER_HORIZON = 2
# Сколько раз один тип блюда (блины, каша, суп) допускается в одном приёме
# пищи за план: один раз на каждые три дня. Цена в целевой функции весит на
# порядок больше разнообразия, поэтому самая дешёвая группа иначе забирает
# все слоты — на завтрак получались три блина подряд.
DISH_TYPE_DAYS_PER_USE = 3

Slot = tuple[int, str]  # (индекс дня, meal_type)


def stable_tiebreak(recipe_id: int, plan_key: str) -> int:
    digest = hashlib.sha256(f"{plan_key}:{recipe_id}".encode()).hexdigest()
    return int(digest[:8], 16)


def dish_type_cap(days: int, distinct_types: int) -> int:
    """Предел повторов одного типа блюда в приёме пищи за горизонт.

    Если разных типов в кандидатах мало, предел ослабляется ровно настолько,
    чтобы слоты можно было заполнить: пустой слот хуже повтора.
    """
    cap = max(1, days // DISH_TYPE_DAYS_PER_USE)
    if distinct_types > 0:
        cap = max(cap, -(-days // distinct_types))
    return cap


def slot_coefficient(
    score: CandidateScore, meal_type: str, cost_factor: int
) -> int:
    """Вклад назначения блюда в целевую функцию (меньше — лучше)."""
    fit = score.meal_fit.get(meal_type, 0.0) + score.meal_bias.get(meal_type, 0.0)
    value = (
        cost_factor * (score.cost_kop // 100)
        + W_TASTE * score.dislike_penalty
        - W_WASTE * score.expiry_bonus
        - W_CUISINE * score.cuisine_bonus
        - int(W_TASTE_AFFINITY * score.affinity)
        + int(W_UNKNOWN * score.unknown)
        + int(W_RECENCY * score.recency_penalty)
        - int(W_SEASON * score.season_bonus)
        - int(W_FIT * fit)
    )
    return value


def _solve_cpsat(
    *,
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    budget_kop: int | None,
    daily_kcal_target: int | None,
    cost_factor: int,
    time_limit_seconds: float,
) -> tuple[dict[Slot, int | None], str] | None:
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None

    model = cp_model.CpModel()
    slots = [(day, meal) for day in range(days) for meal in meal_types]
    x: dict[tuple[int, int, str], Any] = {}
    empty: dict[Slot, Any] = {}
    objective: list[Any] = []

    for day, meal in slots:
        slot_vars = []
        for recipe_id in candidates_by_slot.get((day, meal), []):
            variable = model.NewBoolVar(f"x_{recipe_id}_{day}_{meal}")
            x[recipe_id, day, meal] = variable
            slot_vars.append(variable)
            objective.append(
                slot_coefficient(scores[recipe_id], meal, cost_factor) * variable
            )
        empty_var = model.NewBoolVar(f"empty_{day}_{meal}")
        empty[day, meal] = empty_var
        model.Add(sum(slot_vars) + empty_var == 1)
        objective.append(W_EMPTY_SLOT * empty_var)

    recipe_ids = sorted({recipe_id for recipe_id, _, _ in x})
    usage: dict[tuple[int, int], Any] = {}
    for recipe_id in recipe_ids:
        for day in range(days):
            day_vars = [
                x[recipe_id, day, meal]
                for meal in meal_types
                if (recipe_id, day, meal) in x
            ]
            if not day_vars:
                continue
            used = model.NewBoolVar(f"u_{recipe_id}_{day}")
            model.Add(sum(day_vars) <= 1)
            model.AddMaxEquality(used, day_vars)
            usage[recipe_id, day] = used
        horizon_vars = [
            usage[recipe_id, day] for day in range(days) if (recipe_id, day) in usage
        ]
        if horizon_vars:
            model.Add(sum(horizon_vars) <= MAX_USES_PER_HORIZON)
        for day in range(days - 1):
            if (recipe_id, day) in usage and (recipe_id, day + 1) in usage:
                model.Add(usage[recipe_id, day] + usage[recipe_id, day + 1] <= 1)

    # Разнообразие по типу блюда: один тип занимает не больше dish_type_cap
    # слотов приёма пищи за весь план (жёстко — иначе цена перевешивает).
    for meal in meal_types:
        by_type: dict[str, list[Any]] = {}
        for (recipe_id, _day, slot_meal), variable in x.items():
            if slot_meal != meal:
                continue
            dish_type = scores[recipe_id].dish_type
            if dish_type:
                by_type.setdefault(dish_type, []).append(variable)
        cap = dish_type_cap(days, len(by_type))
        for variables in by_type.values():
            if len(variables) > cap:
                model.Add(sum(variables) <= cap)

    # Разнообразие: главный ингредиент не повторяется в соседние дни.
    mains: dict[str, list[tuple[int, Any]]] = {}
    for (recipe_id, day), used in usage.items():
        main = scores[recipe_id].main_ingredient
        if main:
            mains.setdefault(main, []).append((day, used))
    for main, entries in mains.items():
        by_day: dict[int, list[Any]] = {}
        for day, used in entries:
            by_day.setdefault(day, []).append(used)
        day_flags: dict[int, Any] = {}
        for day, variables in by_day.items():
            flag = model.NewBoolVar(f"main_{abs(hash(main)) % 10**8}_{day}")
            model.AddMaxEquality(flag, variables)
            day_flags[day] = flag
        for day in range(days - 1):
            if day in day_flags and day + 1 in day_flags:
                repeat = model.NewBoolVar(f"rep_{abs(hash(main)) % 10**8}_{day}")
                model.Add(day_flags[day] + day_flags[day + 1] - 1 <= repeat)
                objective.append(W_VARIETY * repeat)

    # Бюджет — мягкое ограничение (жёсткое легко даёт infeasible, TZ §2.3).
    if budget_kop is not None:
        total_cost = sum(
            (scores[recipe_id].cost_kop // 100) * variable
            for (recipe_id, _, _), variable in x.items()
        )
        over = model.NewIntVar(0, 10**7, "budget_over")
        model.Add(over >= total_cost - budget_kop // 100)
        objective.append(W_BUDGET * over)

    # Дневные калории: ±20% мягко.
    if daily_kcal_target:
        low = int(daily_kcal_target * 0.8) // 100
        high = int(daily_kcal_target * 1.2) // 100
        for day in range(days):
            day_kcal = sum(
                ((scores[recipe_id].kcal or 0) // 100) * x[recipe_id, day, meal]
                for meal in meal_types
                for recipe_id in candidates_by_slot.get((day, meal), [])
                if scores[recipe_id].kcal
            )
            if isinstance(day_kcal, int):
                continue  # у кандидатов дня нет оценок калорий
            over = model.NewIntVar(0, 10**6, f"kcal_over_{day}")
            under = model.NewIntVar(0, 10**6, f"kcal_under_{day}")
            model.Add(over >= day_kcal - high)
            model.Add(under >= low - day_kcal)
            objective.append(W_KCAL * (over + under))

    model.Minimize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_workers = SOLVER_WORKERS
    solver.parameters.random_seed = 7
    # План в пределах 2% от оптимума неотличим для пользователя, а доказательство
    # строгой оптимальности съедает весь лимит времени.
    solver.parameters.relative_gap_limit = 0.02
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    assignment: dict[Slot, int | None] = {}
    for day, meal in slots:
        chosen = None
        for recipe_id in candidates_by_slot.get((day, meal), []):
            if solver.Value(x[recipe_id, day, meal]):
                chosen = recipe_id
                break
        assignment[day, meal] = chosen
    return assignment, "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"


def _solve_greedy(
    *,
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    cost_factor: int,
    plan_key: str,
) -> tuple[dict[Slot, int | None], str]:
    """Детерминированный fallback: те же жёсткие ограничения, локальный выбор.

    Кандидатов не хватило — слот остаётся пустым (никаких молчаливых
    повторов, дефект P2)."""
    assignment: dict[Slot, int | None] = {}
    used_by_day: dict[int, set[int]] = {day: set() for day in range(days)}
    use_count: dict[int, int] = {}
    main_by_day: dict[int, set[str]] = {day: set() for day in range(days)}
    dish_type_uses: dict[tuple[str, str], int] = {}
    dish_type_caps = {
        meal: dish_type_cap(
            days,
            len({
                scores[recipe_id].dish_type
                for day in range(days)
                for recipe_id in candidates_by_slot.get((day, meal), [])
                if scores[recipe_id].dish_type
            }),
        )
        for meal in meal_types
    }

    for day in range(days):
        for meal in meal_types:
            best: tuple[int, int, int] | None = None  # (штраф, tiebreak, id)
            for recipe_id in candidates_by_slot.get((day, meal), []):
                if recipe_id in used_by_day[day]:
                    continue
                if use_count.get(recipe_id, 0) >= MAX_USES_PER_HORIZON:
                    continue
                if day > 0 and recipe_id in used_by_day[day - 1]:
                    continue
                score = scores[recipe_id]
                if score.dish_type and dish_type_uses.get(
                    (meal, score.dish_type), 0
                ) >= dish_type_caps[meal]:
                    continue
                penalty = slot_coefficient(score, meal, cost_factor)
                main = score.main_ingredient
                if main and (
                    main in main_by_day[day]
                    or (day > 0 and main in main_by_day[day - 1])
                ):
                    penalty += W_VARIETY
                candidate = (penalty, stable_tiebreak(recipe_id, f"{plan_key}:{day}:{meal}"), recipe_id)
                if best is None or candidate < best:
                    best = candidate
            if best is None:
                assignment[day, meal] = None
                continue
            recipe_id = best[2]
            assignment[day, meal] = recipe_id
            used_by_day[day].add(recipe_id)
            use_count[recipe_id] = use_count.get(recipe_id, 0) + 1
            main = scores[recipe_id].main_ingredient
            if main:
                main_by_day[day].add(main)
            dish_type = scores[recipe_id].dish_type
            if dish_type:
                key = (meal, dish_type)
                dish_type_uses[key] = dish_type_uses.get(key, 0) + 1
    return assignment, "greedy"


def optimize(
    *,
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    budget_kop: int | None = None,
    daily_kcal_target: int | None = None,
    price_tier: str = "balanced",
    plan_key: str = "",
    time_limit_seconds: float = SOLVER_TIME_LIMIT_SECONDS,
) -> tuple[dict[Slot, int | None], str]:
    """Назначение блюд по слотам: CP-SAT, при недоступности/неудаче — жадный."""
    cost_factor = PRICE_TIER_COST_FACTOR.get(price_tier, 10)
    trimmed = {
        slot: candidates[:MAX_CANDIDATES_PER_SLOT]
        for slot, candidates in candidates_by_slot.items()
    }
    solved = _solve_cpsat(
        days=days,
        meal_types=meal_types,
        candidates_by_slot=trimmed,
        scores=scores,
        budget_kop=budget_kop,
        daily_kcal_target=daily_kcal_target,
        cost_factor=cost_factor,
        time_limit_seconds=time_limit_seconds,
    )
    if solved is not None:
        return solved
    return _solve_greedy(
        days=days,
        meal_types=meal_types,
        candidates_by_slot=trimmed,
        scores=scores,
        cost_factor=cost_factor,
        plan_key=plan_key,
    )
