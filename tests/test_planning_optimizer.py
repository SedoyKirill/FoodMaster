"""TZ-M5R §4, тесты 5–7 и 10: модель CP-SAT и жадный fallback."""

import itertools
import os
import sys
import unittest
from datetime import date
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import build_plan
from app.web.planning import optimizer
from app.web.planning.candidates import CandidateScore
from app.web.planning.weights import weights_for

try:
    from ortools.sat.python import cp_model  # noqa: F401
    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


def score(
    recipe_id,
    *,
    cost=0,
    kcal=None,
    fit=1.0,
    cuisine=0,
    dislike=0,
    main=None,
    protein=None,
    unknown=False,
    recency=0.0,
    base="veg",
    cuisine_code=None,
    dish_type=None,
):
    item = CandidateScore(recipe_id)
    item.cost_kop = cost
    item.kcal = kcal
    item.cuisine_bonus = cuisine
    item.dislike_penalty = dislike
    item.main_ingredient = main
    item.protein_g = protein
    item.unknown = unknown
    item.recency_penalty = recency
    item.protein_base = base
    item.cuisine = cuisine_code
    item.dish_type = dish_type
    item.meal_fit = {"breakfast": fit, "lunch": fit, "dinner": fit}
    return item


MEALS = ["breakfast", "lunch", "dinner"]


def run_optimize(days, scores, **kwargs):
    ids = sorted(scores)
    slots = {(day, meal): ids for day in range(days) for meal in MEALS}
    # Кандидаты в тестах различаются только ценой, поэтому модель предельно
    # симметрична и доказательство оптимальности съедает весь лимит. План от
    # этого не меняется — режем время, чтобы набор тестов оставался быстрым.
    kwargs.setdefault("time_limit_seconds", 5.0)
    solution = optimizer.optimize(
        days=days,
        meal_types=MEALS,
        candidates_by_slot=slots,
        scores=scores,
        plan_key="test",
        **kwargs,
    )
    return solution.assignment, solution.status


def assert_repetition_rules(test, assignment, days):
    uses = {}
    for (day, _meal), recipe_id in assignment.items():
        if recipe_id is not None:
            uses.setdefault(recipe_id, []).append(day)
    for recipe_id, day_list in uses.items():
        day_set = sorted(set(day_list))
        test.assertLessEqual(len(day_list), 2, f"рецепт {recipe_id} чаще 2 раз")
        for first, second in itertools.pairwise(day_set):
            test.assertGreater(second - first, 1, f"рецепт {recipe_id} в соседние дни")
        for day in day_set:
            test.assertEqual(day_list.count(day), 1, f"рецепт {recipe_id} дважды в день {day}")


class RepetitionTests(unittest.TestCase):
    """Тест 5: повторяемость на горизонте 7 дней — и CP-SAT, и fallback."""

    def setUp(self) -> None:
        self.scores = {i: score(i, cost=1000 + i) for i in range(1, 13)}

    @unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
    def test_cpsat_respects_repetition_rules(self) -> None:
        assignment, status = run_optimize(7, self.scores)
        self.assertIn(status, {"OPTIMAL", "FEASIBLE"})
        assert_repetition_rules(self, assignment, 7)
        self.assertTrue(all(v is not None for v in assignment.values()))

    def test_greedy_respects_repetition_rules(self) -> None:
        solution = optimizer._solve_greedy(
            days=7,
            meal_types=MEALS,
            candidates_by_slot={(d, m): sorted(self.scores) for d in range(7) for m in MEALS},
            scores=self.scores,
            weights=weights_for("balanced"),
            time_limits={},
            plan_key="test",
        )
        assignment, status = solution.assignment, solution.status
        self.assertEqual(status, "greedy")
        assert_repetition_rules(self, assignment, 7)
        self.assertTrue(all(v is not None for v in assignment.values()))


@unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
class BudgetTests(unittest.TestCase):
    def test_low_budget_gives_cheapest_feasible_plan(self) -> None:
        """Тест 6: план строится, стоимость минимальна среди допустимых."""
        costs = {1: 10000, 2: 20000, 3: 30000, 4: 40000, 5: 50000}
        scores = {i: score(i, cost=c) for i, c in costs.items()}
        assignment, status = run_optimize(1, scores, budget_kop=100)
        self.assertIn(status, {"OPTIMAL", "FEASIBLE"})
        chosen = [v for v in assignment.values() if v is not None]
        self.assertEqual(len(chosen), 3)
        self.assertEqual(len(set(chosen)), 3)
        total = sum(costs[i] for i in chosen)
        best = min(
            sum(costs[i] for i in combo)
            for combo in itertools.combinations(costs, 3)
        )
        self.assertEqual(total, best)

    def test_budget_warning_in_plan(self) -> None:
        recipes = []
        for index, meal in enumerate(["breakfast", "lunch", "dinner"], start=1):
            recipes.append(
                {
                    "id": index,
                    "title": f"Блюдо {index}",
                    "source_page_start": index,
                    "source_servings_min": Decimal("1"),
                    "cuisine_code": None,
                    "meal_types": [meal],
                    "appliances": [],
                    "review_status": "needs_review",
                    "extraction_confidence": Decimal("0.9"),
                    "ingredients": [
                        {
                            "ingredient_text": "Молоко",
                            "normalized_name": "молоко",
                            "quantity_min": Decimal("1000"),
                            "quantity_max": Decimal("1000"),
                            "unit_code": "ml",
                        }
                    ],
                }
            )
        plan = build_plan(
            household_id="household",
            starts_on=date(2026, 8, 17),
            days=1,
            cuisines=[],
            people=[{"name": "Взрослый", "person_type": "adult", "portion_factor": Decimal("1")}],
            appliances=[],
            rules=[],
            inventory=[],
            recipes=recipes,
            products=[
                {"id": 1, "name": "Молоко пастеризованное", "pack_quantity": Decimal("1000"),
                 "pack_unit": "ml", "effective_price_kop": 9999}
            ],
            budget_kop=100,
        )
        self.assertTrue(any(w.startswith("budget_exceeded") for w in plan["warnings"]))
        self.assertEqual(len(plan["meals"]), 3)


@unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
class ToyOptimalityTests(unittest.TestCase):
    """Тест 7: дешёвое блюдо при W_COST-доминировании, любимая кухня — при W_CUISINE."""

    def test_cost_dominates_in_economy(self) -> None:
        scores = {
            1: score(1, cost=50000),
            2: score(2, cost=5000),
            3: score(3, cost=30000),
        }
        slots = {(0, "dinner"): [1, 2, 3]}
        solution = optimizer.optimize(
            days=1, meal_types=["dinner"], candidates_by_slot=slots,
            scores=scores, mode="economy", plan_key="toy",
        )
        self.assertEqual(solution.assignment[0, "dinner"], 2)

    def test_cuisine_wins_when_costs_equal(self) -> None:
        scores = {
            1: score(1, cost=10000, cuisine=0),
            2: score(2, cost=10000, cuisine=1),
            3: score(3, cost=10000, cuisine=0),
        }
        slots = {(0, "dinner"): [1, 2, 3]}
        solution = optimizer.optimize(
            days=1, meal_types=["dinner"], candidates_by_slot=slots,
            scores=scores, plan_key="toy",
        )
        self.assertEqual(solution.assignment[0, "dinner"], 2)


@unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
class ModeTests(unittest.TestCase):
    """TZ-M8 §6.4: режим семьи — это другие числа, а не подсказка в форме."""

    @staticmethod
    def _dinner(scores, **kwargs):
        solution = optimizer.optimize(
            days=1,
            meal_types=["dinner"],
            candidates_by_slot={(0, "dinner"): sorted(scores)},
            scores=scores,
            plan_key="mode",
            **kwargs,
        )
        return solution.assignment[0, "dinner"]

    def test_variety_prefers_the_untried_dish_economy_the_familiar_one(self) -> None:
        """Знакомое блюдо ели вчера; новое — дороже и без мнения семьи."""
        scores = {
            1: score(1, cost=20000, recency=1.0),
            2: score(2, cost=22000, unknown=True),
        }
        self.assertEqual(self._dinner(scores, mode="variety"), 2)
        self.assertEqual(self._dinner(scores, mode="economy"), 1)

    def test_slot_kcal_target_beats_the_family_sum(self) -> None:
        """Цель считается по едокам слота: завтрак на одного — не день на троих."""
        scores = {1: score(1, cost=20000, kcal=600), 2: score(2, cost=20000, kcal=2000)}
        chosen = self._dinner(
            scores, slot_targets={(0, "dinner"): optimizer.SlotTarget(kcal=600)}
        )
        self.assertEqual(chosen, 1)

    def test_fitness_pays_for_protein_economy_does_not(self) -> None:
        scores = {
            1: score(1, cost=25000, protein=40),
            2: score(2, cost=20000, protein=5),
        }
        targets = {(0, "dinner"): optimizer.SlotTarget(protein_g=40)}
        self.assertEqual(self._dinner(scores, mode="fitness", slot_targets=targets), 1)
        self.assertEqual(self._dinner(scores, mode="economy", slot_targets=targets), 2)

    def test_unknown_protein_is_imputed_not_counted_as_zero(self) -> None:
        """Блюдо без разметки КБЖУ не должно проигрывать из-за отсутствия данных."""
        scores = {1: score(1, cost=20000, protein=40), 2: score(2, cost=19000)}
        chosen = self._dinner(
            scores,
            mode="fitness",
            slot_targets={(0, "dinner"): optimizer.SlotTarget(protein_g=40)},
        )
        self.assertEqual(chosen, 2)


@unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
class LeftoverTests(unittest.TestCase):
    """«Готовим один раз — едим дважды» (TZ-M8 §6.2).

    Обеды в этих тестах дороже ужинов: иначе выгода от остатка сводится к
    одному бонусу и тонет в двухпроцентном зазоре решателя.
    """

    MEALS = ["lunch", "dinner"]

    def _solve(self, dish_type="soup", days=2, **kwargs):
        dinners = {
            index: score(index, cost=20000, dish_type=dish_type)
            for index in range(1, 5 + days)
        }
        lunches = {
            index: score(index, cost=40000, dish_type="salad")
            for index in range(100, 105 + days)
        }
        scores = {**dinners, **lunches}
        slots = {}
        for day in range(days):
            slots[day, "dinner"] = sorted(dinners)
            slots[day, "lunch"] = sorted(lunches)
        return optimizer.optimize(
            days=days,
            meal_types=self.MEALS,
            candidates_by_slot=slots,
            scores=scores,
            plan_key="left",
            time_limit_seconds=5.0,
            **kwargs,
        )

    def test_soup_cooked_for_dinner_closes_the_next_lunch(self) -> None:
        solution = self._solve(allow_leftovers=True)
        self.assertEqual(len(solution.leftovers), 1)
        heir, source = next(iter(solution.leftovers.items()))
        self.assertEqual(heir, (1, "lunch"))
        self.assertEqual(source, (0, "dinner"))
        self.assertEqual(solution.assignment[heir], solution.assignment[source])

    def test_salad_is_not_reheated_tomorrow(self) -> None:
        """Список LEFTOVER_FRIENDLY — не украшение: салат ночь не переживает."""
        self.assertEqual(self._solve(dish_type="salad", allow_leftovers=True).leftovers, {})

    def test_leftovers_are_off_by_default(self) -> None:
        self.assertEqual(self._solve().leftovers, {})

    def test_no_more_than_one_pair_per_three_days(self) -> None:
        """Выгодно закрыть остатками все обеды, но так не едят."""
        solution = self._solve(days=4, allow_leftovers=True)
        self.assertEqual(len(solution.leftovers), 2)

    def test_an_expensive_dinner_is_not_worth_cooking_twice(self) -> None:
        """Экономия — не самоцель: лишняя порция дорогого ужина дороже обеда."""
        dinners = {index: score(index, cost=90000, dish_type="soup") for index in range(1, 7)}
        lunches = {index: score(index, cost=40000, dish_type="salad") for index in range(100, 106)}
        scores = {**dinners, **lunches}
        slots = {}
        for day in range(2):
            slots[day, "dinner"] = sorted(dinners)
            slots[day, "lunch"] = sorted(lunches)
        solution = optimizer.optimize(
            days=2, meal_types=self.MEALS, candidates_by_slot=slots, scores=scores,
            plan_key="left", time_limit_seconds=5.0, allow_leftovers=True,
        )
        self.assertEqual(solution.leftovers, {})


@unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
class VarietyTests(unittest.TestCase):
    """Разнообразие белковой базы и кухни в ужинах (TZ-M8 §6.2)."""

    def _dinners(self, scores, days, **kwargs):
        slots = {(day, "dinner"): sorted(scores) for day in range(days)}
        solution = optimizer.optimize(
            days=days,
            meal_types=["dinner"],
            candidates_by_slot=slots,
            scores=scores,
            plan_key="variety",
            time_limit_seconds=5.0,
            **kwargs,
        )
        return [solution.assignment[day, "dinner"] for day in range(days)]

    def test_meat_cannot_take_every_dinner_of_the_week(self) -> None:
        """Котлеты, гуляш и жаркое — три типа блюда и одно мясо."""
        scores = {index: score(index, cost=10000, base="meat") for index in range(1, 8)}
        scores.update(
            {index: score(index, cost=30000, base="veg") for index in range(8, 15)}
        )
        chosen = self._dinners(scores, 7)
        meat = sum(1 for recipe_id in chosen if scores[recipe_id].protein_base == "meat")
        self.assertLessEqual(meat, optimizer.protein_base_cap(7, 2))

    def test_cap_relaxes_when_there_is_nothing_else(self) -> None:
        """Библиотека из одного мяса не должна оставлять слоты пустыми."""
        scores = {index: score(index, cost=10000, base="meat") for index in range(1, 15)}
        chosen = self._dinners(scores, 7)
        self.assertTrue(all(recipe_id is not None for recipe_id in chosen))

    def test_three_italian_dinners_in_a_row_are_penalised(self) -> None:
        scores = {
            index: score(index, cost=10000, cuisine_code="italian")
            for index in range(1, 8)
        }
        scores.update(
            {
                index: score(index, cost=12000, cuisine_code="russian")
                for index in range(8, 15)
            }
        )
        chosen = self._dinners(scores, 3, cuisine_mode="prefer")
        self.assertNotEqual(
            [scores[recipe_id].cuisine for recipe_id in chosen], ["italian"] * 3
        )

    def test_hard_cuisine_filter_is_not_penalised_for_being_narrow(self) -> None:
        """При cuisine_mode=only выбор и так узкий — штрафовать нечестно."""
        scores = {
            index: score(index, cost=10000, cuisine_code="italian")
            for index in range(1, 8)
        }
        scores.update(
            {
                index: score(index, cost=12000, cuisine_code="russian")
                for index in range(8, 15)
            }
        )
        chosen = self._dinners(scores, 3, cuisine_mode="only")
        self.assertEqual(
            [scores[recipe_id].cuisine for recipe_id in chosen], ["italian"] * 3
        )


@unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
class StabilityTests(unittest.TestCase):
    """Приёмка §9.2: та же семья и дата — тот же план.

    Лимит по стенным часам этого не давал: сколько узлов успеет солвер,
    зависит от загрузки машины, и на равных кандидатах меню менялось от
    запуска к запуску. Пересборка «того же самого» не должна показывать
    другое меню — это выглядит как случайный подбор.
    """

    def test_same_input_gives_the_same_plan(self) -> None:
        plans = set()
        for _ in range(5):
            scores = {index: score(index, cost=20000) for index in range(1, 25)}
            assignment, _status = run_optimize(5, scores)
            plans.add(tuple(assignment[slot] for slot in sorted(assignment)))
        self.assertEqual(len(plans), 1)


class NoveltyCapTests(unittest.TestCase):
    """Сколько непробованного попадает в план (TZ-M8 §4.5)."""

    def test_share_of_slots_by_level(self) -> None:
        self.assertEqual(optimizer.novelty_cap("low", 21), 3)
        self.assertEqual(optimizer.novelty_cap("medium", 21), 7)
        self.assertEqual(optimizer.novelty_cap("high", 21), 13)

    def test_unknown_level_falls_back_to_medium(self) -> None:
        self.assertEqual(optimizer.novelty_cap("странно", 21), 7)

    def test_slots_without_a_familiar_candidate_are_not_forced_empty(self) -> None:
        self.assertEqual(optimizer.novelty_cap("low", 21, unavoidable=9), 9)

    @unittest.skipUnless(HAS_ORTOOLS, "ortools не установлен")
    def test_new_dishes_do_not_take_over_the_plan(self) -> None:
        scores = {index: score(index, cost=10000, unknown=True) for index in range(1, 8)}
        scores.update(
            {index: score(index, cost=30000, unknown=False) for index in range(8, 15)}
        )
        slots = {(day, "dinner"): sorted(scores) for day in range(6)}
        solution = optimizer.optimize(
            days=6, meal_types=["dinner"], candidates_by_slot=slots, scores=scores,
            novelty="low", plan_key="novelty", time_limit_seconds=5.0,
        )
        new_dishes = sum(
            1
            for day in range(6)
            if scores[solution.assignment[day, "dinner"]].unknown
        )
        self.assertLessEqual(new_dishes, optimizer.novelty_cap("low", 6))


class FallbackTests(unittest.TestCase):
    def test_fallback_without_ortools_gives_valid_plan(self) -> None:
        """Тест 10: ImportError → жадный алгоритм, план валиден и без повторов."""
        scores = {i: score(i, cost=1000 * i) for i in range(1, 13)}
        slots = {(d, m): sorted(scores) for d in range(7) for m in MEALS}
        with mock.patch.dict(sys.modules, {"ortools": None, "ortools.sat.python": None}):
            solution = optimizer.optimize(
                days=7, meal_types=MEALS, candidates_by_slot=slots,
                scores=scores, plan_key="fallback",
            )
        self.assertEqual(solution.status, "greedy")
        assert_repetition_rules(self, solution.assignment, 7)
        self.assertTrue(all(v is not None for v in solution.assignment.values()))

    def test_greedy_leaves_slot_empty_instead_of_repeating(self) -> None:
        scores = {1: score(1, cost=1000)}
        slots = {(d, m): [1] for d in range(2) for m in MEALS}
        solution = optimizer._solve_greedy(
            days=2, meal_types=MEALS, candidates_by_slot=slots,
            scores=scores, weights=weights_for("balanced"), time_limits={},
            plan_key="scarce",
        )
        filled = [v for v in solution.assignment.values() if v is not None]
        self.assertEqual(len(filled), 1)  # день 2 соседний — повтор запрещён


if __name__ == "__main__":
    unittest.main()
