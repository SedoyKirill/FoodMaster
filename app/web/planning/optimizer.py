"""Оптимизация меню по горизонту: CP-SAT (OR-Tools) + жадный fallback.

TZ-M5R §2.3. Все веса — именованные константы в одном месте. Модель
минимизирует стоимость/штрафы; пустой слот допустим, но наказан так, что
выбирается только при реальной нехватке кандидатов (и честно попадает в
warnings, а не молча повторяет блюдо — дефект P2 старого планировщика).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .candidates import CandidateScore
from .weights import WeightProfile, weights_for

# Веса целевой функции переехали в weights.py: до M8 набор был один на всех,
# теперь семья выбирает режим, и «экономно» отличается от «разнообразно» не
# подсказкой в интерфейсе, а числами (TZ-M8 §6.4). Здесь остаются только
# параметры самого решателя.
SOLVER_TIME_LIMIT_SECONDS = 10.0
#: потолок времени решателя: две недели с упаковками должны укладываться (§6.5)
SOLVER_TIME_LIMIT_MAX_SECONDS = 30.0
SOLVER_WORKERS = 4
MAX_CANDIDATES_PER_SLOT = 40
#: на горизонте длиннее недели список кандидатов режется: переменных иначе
#: втрое больше, а выбор от сорокового кандидата уже не меняется (§6.5)
MAX_CANDIDATES_PER_SLOT_LONG = 30
LONG_HORIZON_DAYS = 7
MAX_USES_PER_HORIZON = 2
# Сколько раз один тип блюда (блины, каша, суп) допускается в одном приёме
# пищи за план: один раз на каждые три дня. Цена в целевой функции весит на
# порядок больше разнообразия, поэтому самая дешёвая группа иначе забирает
# все слоты — на завтрак получались три блина подряд.
DISH_TYPE_DAYS_PER_USE = 3
# Белковая база (мясо, птица, рыба…) ограничивается так же, как тип блюда:
# «мы всю неделю едим курицу» — самая частая жалоба на автоменю.
PROTEIN_BASE_DAYS_PER_USE = 3
#: базы, которые ограничиваются жёстко (§6.2)
HARD_LIMITED_BASES = ("meat", "poultry", "fish")
#: сколько дней подряд одна кухня в ужинах терпима без штрафа (§6.2)
CUISINE_STREAK = 3
#: доля слотов под непробованные блюда по уровню новизны (§4.5)
NOVELTY_SHARES = {"low": 0.1, "medium": 0.3, "high": 0.6}
#: Что переживает ночь в холодильнике и не портится от разогрева (§6.2).
#: Салат и яичница в этот список не входят намеренно.
LEFTOVER_FRIENDLY = frozenset({
    "soup", "stew", "pilaf", "casserole", "pasta", "cutlets", "porridge",
    "risotto", "curry", "goulash", "roast", "chili", "lasagna",
})
#: сколько пар «ужин → обед» допускается на горизонте
LEFTOVER_DAYS_PER_PAIR = 3

Slot = tuple[int, str]  # (индекс дня, meal_type)

#: Коридор отклонения от цели по калориям: слот держится мягче дня — ужин
#: бывает плотнее обеда, а вот день, вылетевший на четверть, семья замечает.
KCAL_SLOT_TOLERANCE = 0.15
KCAL_DAY_TOLERANCE = 0.10


@dataclass(frozen=True)
class SlotTarget:
    """Цель слота по калориям и белку — сумма норм тех, кто его ест дома."""

    kcal: int = 0
    protein_g: int = 0


@dataclass
class Solution:
    """Решение по горизонту: назначения и то, что к ним прилагается.

    До M8 хватало пары «назначения, статус». С остатками «на два раза» слот
    может быть занят не выбором, а вчерашним ужином, и планировщику нужно
    знать, каким именно.
    """

    assignment: dict[Slot, int | None]
    status: str
    #: слот-наследник → слот-источник («обед среды — это ужин вторника»)
    leftovers: dict[Slot, Slot] = field(default_factory=dict)
    #: товар → число упаковок из модели упаковок (T7)
    packs: dict[int, int] = field(default_factory=dict)


def protein_base_cap(days: int, distinct_bases: int) -> int:
    """Предел ужинов на одной белковой базе за горизонт (§6.2).

    Как и с типом блюда: если баз в кандидатах мало, предел ослабляется —
    пустой слот хуже третьего куриного ужина.
    """
    cap = max(1, days // PROTEIN_BASE_DAYS_PER_USE) + 1
    if distinct_bases > 0:
        cap = max(cap, -(-days // distinct_bases))
    return cap


def novelty_cap(novelty: str | None, slots: int, unavoidable: int = 0) -> int:
    """Сколько слотов отдаётся блюдам, о которых у семьи нет мнения (§4.5).

    ``unavoidable`` — слоты, где знакомых кандидатов нет вовсе: запрещать там
    новое означало бы оставить слот пустым.
    """
    share = NOVELTY_SHARES.get(str(novelty or "medium"), NOVELTY_SHARES["medium"])
    return max(-(-int(share * slots * 100) // 100), unavoidable)


def candidates_per_slot(days: int) -> int:
    """Сколько кандидатов слота попадает в модель (§6.5)."""
    return (
        MAX_CANDIDATES_PER_SLOT_LONG
        if days > LONG_HORIZON_DAYS
        else MAX_CANDIDATES_PER_SLOT
    )


def solver_time_limit(days: int) -> float:
    """Лимит решателя растёт линейно с горизонтом и упирается в потолок."""
    return min(SOLVER_TIME_LIMIT_SECONDS + 2 * days, SOLVER_TIME_LIMIT_MAX_SECONDS)


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
    score: CandidateScore,
    meal_type: str,
    weights: WeightProfile,
    time_limit: int | None = None,
) -> int:
    """Вклад назначения блюда в целевую функцию (меньше — лучше).

    Единственная формула на солвер, жадный запасной алгоритм, ранжирование
    кандидатов и список замен (TZ-M5R §2.3, TZ-M8 §6.1). Веса приходят из
    режима семьи; ``time_limit`` — мягкая граница времени для этого слота.
    """
    fit = score.meal_fit.get(meal_type, 0.0) + score.meal_bias.get(meal_type, 0.0)
    over_minutes = 0
    if time_limit and score.time_minutes:
        over_minutes = max(0, int(score.time_minutes) - int(time_limit))
    value = (
        weights.cost * (score.cost_kop // 100)
        + int(weights.dislike * score.dislike_penalty)
        - weights.waste * score.expiry_bonus
        - weights.cuisine * score.cuisine_bonus
        - int(weights.taste * score.affinity)
        + int(weights.unknown * score.unknown)
        + int(weights.recency * score.recency_penalty)
        + weights.time * (over_minutes // 15)
        + (weights.time_unknown if score.time_minutes is None else 0)
        - int(weights.season * score.season_bonus)
        - int(weights.fit * fit)
    )
    return value


def _slot_protein(
    scores: dict[int, CandidateScore], candidates: list[int]
) -> dict[int, int]:
    """Белок кандидатов слота; неизвестный — медиана известных, а не ноль.

    Иначе блюда без разметки КБЖУ выглядели бы «безбелковыми» и в режиме
    «фитнес» проигрывали бы всё подряд просто потому, что о них нет данных.
    """
    known = sorted(
        scores[recipe_id].protein_g
        for recipe_id in candidates
        if scores[recipe_id].protein_g is not None
    )
    if not known:
        return {}
    median = known[len(known) // 2]
    return {
        recipe_id: (
            scores[recipe_id].protein_g
            if scores[recipe_id].protein_g is not None
            else median
        )
        for recipe_id in candidates
    }


def _add_kcal_terms(
    *,
    model: Any,
    objective: list[Any],
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    slot_targets: dict[Slot, SlotTarget],
    x: dict[tuple[int, int, str], Any],
    weights: WeightProfile,
) -> None:
    """Мягкие коридоры калорий: ±15 % на слот, ±10 % на день (§6.2)."""
    for day in range(days):
        day_terms: list[Any] = []
        day_target = 0
        for meal in meal_types:
            target = slot_targets.get((day, meal))
            if target is None or not target.kcal:
                continue
            slot_kcal = sum(
                ((scores[recipe_id].kcal or 0) // 100) * x[recipe_id, day, meal]
                for recipe_id in candidates_by_slot.get((day, meal), [])
                if scores[recipe_id].kcal
            )
            if isinstance(slot_kcal, int):
                continue  # у кандидатов слота нет оценок калорий
            day_terms.append(slot_kcal)
            day_target += target.kcal
            low = int(target.kcal * (1 - KCAL_SLOT_TOLERANCE)) // 100
            high = int(target.kcal * (1 + KCAL_SLOT_TOLERANCE)) // 100
            over = model.NewIntVar(0, 10**6, f"kcal_over_{day}_{meal}")
            under = model.NewIntVar(0, 10**6, f"kcal_under_{day}_{meal}")
            model.Add(over >= slot_kcal - high)
            model.Add(under >= low - slot_kcal)
            objective.append(weights.kcal * (over + under))
        if not day_terms or not day_target:
            continue
        low = int(day_target * (1 - KCAL_DAY_TOLERANCE)) // 100
        high = int(day_target * (1 + KCAL_DAY_TOLERANCE)) // 100
        over = model.NewIntVar(0, 10**6, f"kcal_over_day_{day}")
        under = model.NewIntVar(0, 10**6, f"kcal_under_day_{day}")
        model.Add(over >= sum(day_terms) - high)
        model.Add(under >= low - sum(day_terms))
        objective.append(weights.kcal * (over + under))


def _add_protein_terms(
    *,
    model: Any,
    objective: list[Any],
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    slot_targets: dict[Slot, SlotTarget],
    x: dict[tuple[int, int, str], Any],
    weights: WeightProfile,
) -> None:
    """Недобор белка за день (§6.2): перебор белка не штрафуется."""
    for day in range(days):
        day_terms: list[Any] = []
        day_target = 0
        for meal in meal_types:
            target = slot_targets.get((day, meal))
            if target is None or not target.protein_g:
                continue
            candidates = candidates_by_slot.get((day, meal), [])
            protein = _slot_protein(scores, candidates)
            if not protein:
                continue
            day_terms.append(
                sum(protein[recipe_id] * x[recipe_id, day, meal] for recipe_id in candidates)
            )
            day_target += target.protein_g
        if not day_terms or not day_target:
            continue
        # Вес задан «за 10 г недобора», поэтому переменная считает десятки:
        # 10·under ≥ дефицит даёт округление вверх без деления в модели.
        under = model.NewIntVar(0, 10**5, f"protein_under_{day}")
        model.Add(10 * under >= day_target - sum(day_terms))
        objective.append(weights.protein * under)


def _solve_cpsat(
    *,
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    budget_kop: int | None,
    slot_targets: dict[Slot, SlotTarget],
    weights: WeightProfile,
    time_limits: dict[Slot, int | None],
    time_limit_seconds: float,
    max_repeats: int,
    allow_leftovers: bool,
    novelty: str | None,
    cuisine_mode: str,
    leftover_cost_share: float,
) -> Solution | None:
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
        for recipe_id in candidates_by_slot.get((day, meal), []):
            variable = model.NewBoolVar(f"x_{recipe_id}_{day}_{meal}")
            x[recipe_id, day, meal] = variable
            objective.append(
                slot_coefficient(
                    scores[recipe_id], meal, weights, time_limits.get((day, meal))
                )
                * variable
            )
        empty[day, meal] = model.NewBoolVar(f"empty_{day}_{meal}")
        objective.append(weights.empty_slot * empty[day, meal])

    # «Готовим один раз — едим дважды» (§6.2): ужин дня d закрывает обед дня
    # d+1. Переменные заводятся до уравнений слотов — обед-наследник занимает
    # слот вместо выбора, а не вдобавок к нему.
    left: dict[tuple[int, int], Any] = {}
    if allow_leftovers and "dinner" in meal_types and "lunch" in meal_types:
        for day in range(days - 1):
            for recipe_id in candidates_by_slot.get((day, "dinner"), []):
                if scores[recipe_id].dish_type not in LEFTOVER_FRIENDLY:
                    continue
                variable = model.NewBoolVar(f"left_{recipe_id}_{day}")
                left[recipe_id, day] = variable
                model.Add(variable <= x[recipe_id, day, "dinner"])
                # Одно и то же блюдо на обед из вчерашнего и на сегодня
                # заново — это не «на два раза», а повтор.
                same_day = [
                    x[recipe_id, day + 1, meal]
                    for meal in meal_types
                    if (recipe_id, day + 1, meal) in x
                ]
                if same_day:
                    model.Add(variable + sum(same_day) <= 1)
                # Остаток не бесплатный: ужин-источник готовится и на обед,
                # то есть продуктов на него уходит больше. Без этого члена
                # солвер закрывал бы остатками все обеды подряд.
                extra_cost = int(
                    weights.cost
                    * (scores[recipe_id].cost_kop // 100)
                    * leftover_cost_share
                )
                objective.append((extra_cost - weights.leftover) * variable)
        if left:
            model.Add(sum(left.values()) <= max(1, -(-days // LEFTOVER_DAYS_PER_PAIR)))

    for day, meal in slots:
        slot_vars = [
            x[recipe_id, day, meal]
            for recipe_id in candidates_by_slot.get((day, meal), [])
        ]
        inherited = (
            [variable for (_id, source_day), variable in left.items() if source_day == day - 1]
            if meal == "lunch"
            else []
        )
        model.Add(sum(slot_vars) + sum(inherited) + empty[day, meal] == 1)

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
            model.Add(sum(horizon_vars) <= max_repeats)
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
                objective.append(weights.variety * repeat)

    # Разнообразие белковой базы в ужинах (§6.2). Ограничение типа блюда
    # этого не ловит: котлеты, гуляш и жаркое — три разных типа и одно мясо.
    if "dinner" in meal_types:
        base_days: dict[str, dict[int, list[Any]]] = {}
        for (recipe_id, day, meal), variable in x.items():
            if meal != "dinner":
                continue
            base_days.setdefault(scores[recipe_id].protein_base, {}).setdefault(
                day, []
            ).append(variable)
        cap = protein_base_cap(days, len(base_days))
        for base, by_day in base_days.items():
            flags: dict[int, Any] = {}
            for day, variables in by_day.items():
                flag = model.NewBoolVar(f"base_{base}_{day}")
                model.AddMaxEquality(flag, variables)
                flags[day] = flag
            if base in HARD_LIMITED_BASES and days >= 5 and len(flags) > cap:
                model.Add(sum(flags.values()) <= cap)
                continue
            # Остальные базы — мягко: два овощных ужина подряд не беда.
            for day in range(days - 1):
                if day in flags and day + 1 in flags:
                    repeat = model.NewBoolVar(f"base_rep_{base}_{day}")
                    model.Add(flags[day] + flags[day + 1] - 1 <= repeat)
                    objective.append(weights.variety * repeat)

    # Кухня как предпочтение (§6.2): три одинаковых ужина подряд надоедают
    # даже тому, кто выбрал итальянскую кухню. При жёстком фильтре (only)
    # выбор и так узкий — штрафовать за него нечестно.
    if cuisine_mode == "prefer" and "dinner" in meal_types and days >= CUISINE_STREAK:
        cuisine_days: dict[str, dict[int, list[Any]]] = {}
        for (recipe_id, day, meal), variable in x.items():
            cuisine = scores[recipe_id].cuisine
            if meal != "dinner" or not cuisine:
                continue
            cuisine_days.setdefault(cuisine, {}).setdefault(day, []).append(variable)
        for cuisine, by_day in cuisine_days.items():
            flags = {}
            for day, variables in by_day.items():
                flag = model.NewBoolVar(f"cuisine_{abs(hash(cuisine)) % 10**8}_{day}")
                model.AddMaxEquality(flag, variables)
                flags[day] = flag
            for day in range(days - CUISINE_STREAK + 1):
                streak = [flags[day + offset] for offset in range(CUISINE_STREAK) if day + offset in flags]
                if len(streak) < CUISINE_STREAK:
                    continue
                repeat = model.NewBoolVar(f"cuisine_rep_{abs(hash(cuisine)) % 10**8}_{day}")
                model.Add(sum(streak) - (CUISINE_STREAK - 1) <= repeat)
                objective.append(weights.variety * repeat)

    # Новизна (§4.5): и семье с историей нельзя застревать на пятнадцати
    # блюдах, и новичку нельзя выдавать сплошь непробованное.
    unknown_vars = [
        variable
        for (recipe_id, _day, _meal), variable in x.items()
        if scores[recipe_id].unknown
    ]
    if unknown_vars:
        unavoidable = sum(
            1
            for slot in slots
            if candidates_by_slot.get(slot)
            and all(scores[recipe_id].unknown for recipe_id in candidates_by_slot[slot])
        )
        model.Add(sum(unknown_vars) <= novelty_cap(novelty, len(slots), unavoidable))

    # Бюджет — мягкое ограничение (жёсткое легко даёт infeasible, TZ §2.3).
    if budget_kop is not None:
        total_cost = sum(
            (scores[recipe_id].cost_kop // 100) * variable
            for (recipe_id, _, _), variable in x.items()
        )
        over = model.NewIntVar(0, 10**7, "budget_over")
        model.Add(over >= total_cost - budget_kop // 100)
        objective.append(weights.budget * over)

    # Калории по слоту и по дню (TZ-M8 §6.2). Раньше цель была одна на семью
    # и на день: ужин на троих и завтрак на одного оценивались одинаково, и
    # «день сошёлся» мог означать «обед вдвое, ужин пустой».
    _add_kcal_terms(
        model=model,
        objective=objective,
        days=days,
        meal_types=meal_types,
        candidates_by_slot=candidates_by_slot,
        scores=scores,
        slot_targets=slot_targets,
        x=x,
        weights=weights,
    )
    _add_protein_terms(
        model=model,
        objective=objective,
        days=days,
        meal_types=meal_types,
        candidates_by_slot=candidates_by_slot,
        scores=scores,
        slot_targets=slot_targets,
        x=x,
        weights=weights,
    )

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
    leftovers: dict[Slot, Slot] = {}
    for (recipe_id, day), variable in left.items():
        if solver.Value(variable):
            assignment[day + 1, "lunch"] = recipe_id
            leftovers[day + 1, "lunch"] = (day, "dinner")
    return Solution(
        assignment=assignment,
        status="OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        leftovers=leftovers,
    )


def _solve_greedy(
    *,
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    weights: WeightProfile,
    time_limits: dict[Slot, int | None],
    plan_key: str,
    max_repeats: int = MAX_USES_PER_HORIZON,
) -> Solution:
    """Детерминированный fallback: те же жёсткие ограничения, локальный выбор.

    Кандидатов не хватило — слот остаётся пустым (никаких молчаливых
    повторов, дефект P2). Остатки «на два раза» и лимит новизны сюда не
    переносятся: это выбор по горизонту, а жадный алгоритм видит один слот."""
    assignment: dict[Slot, int | None] = {}
    used_by_day: dict[int, set[int]] = {day: set() for day in range(days)}
    use_count: dict[int, int] = {}
    main_by_day: dict[int, set[str]] = {day: set() for day in range(days)}
    base_by_day: dict[int, set[str]] = {day: set() for day in range(days)}
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
                if use_count.get(recipe_id, 0) >= max_repeats:
                    continue
                if day > 0 and recipe_id in used_by_day[day - 1]:
                    continue
                score = scores[recipe_id]
                if score.dish_type and dish_type_uses.get(
                    (meal, score.dish_type), 0
                ) >= dish_type_caps[meal]:
                    continue
                penalty = slot_coefficient(
                    score, meal, weights, time_limits.get((day, meal))
                )
                main = score.main_ingredient
                if main and (
                    main in main_by_day[day]
                    or (day > 0 and main in main_by_day[day - 1])
                ):
                    penalty += weights.variety
                if meal == "dinner" and day > 0 and score.protein_base in base_by_day[day - 1]:
                    penalty += weights.variety
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
            if meal == "dinner":
                base_by_day[day].add(scores[recipe_id].protein_base)
            dish_type = scores[recipe_id].dish_type
            if dish_type:
                key = (meal, dish_type)
                dish_type_uses[key] = dish_type_uses.get(key, 0) + 1
    return Solution(assignment=assignment, status="greedy")


def optimize(
    *,
    days: int,
    meal_types: list[str],
    candidates_by_slot: dict[Slot, list[int]],
    scores: dict[int, CandidateScore],
    budget_kop: int | None = None,
    slot_targets: dict[Slot, SlotTarget] | None = None,
    mode: str | None = None,
    weights: WeightProfile | None = None,
    time_limits: dict[Slot, int | None] | None = None,
    plan_key: str = "",
    time_limit_seconds: float | None = None,
    max_repeats: int = MAX_USES_PER_HORIZON,
    allow_leftovers: bool = False,
    novelty: str | None = None,
    cuisine_mode: str = "only",
    leftover_cost_share: float = 1.0,
) -> Solution:
    """Назначение блюд по слотам: CP-SAT, при недоступности/неудаче — жадный."""
    profile = weights or weights_for(mode)
    targets = slot_targets or {}
    limits = time_limits or {}
    trimmed = {
        slot: candidates[:candidates_per_slot(days)]
        for slot, candidates in candidates_by_slot.items()
    }
    solved = _solve_cpsat(
        days=days,
        meal_types=meal_types,
        candidates_by_slot=trimmed,
        scores=scores,
        budget_kop=budget_kop,
        slot_targets=targets,
        weights=profile,
        time_limits=limits,
        time_limit_seconds=time_limit_seconds or solver_time_limit(days),
        max_repeats=max_repeats,
        allow_leftovers=allow_leftovers,
        novelty=novelty,
        cuisine_mode=cuisine_mode,
        leftover_cost_share=leftover_cost_share,
    )
    if solved is not None:
        return solved
    return _solve_greedy(
        days=days,
        meal_types=meal_types,
        candidates_by_slot=trimmed,
        scores=scores,
        weights=profile,
        time_limits=limits,
        plan_key=plan_key,
        max_repeats=max_repeats,
    )
