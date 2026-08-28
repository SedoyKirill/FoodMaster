"""Запросы ``AppRepository`` через фейковый пул (TZ-TESTS §3.4).

Здесь проверяется SQL и логика репозитория: какой запрос ушёл, с какими
параметрами и сколько раз. HTTP-обвязка — в ``test_web_api.py``.
"""

from __future__ import annotations

import os
import secrets
import sys
import unittest
import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web import database as db_module  # noqa: E402
from app.web.database import AppRepository, row_dict  # noqa: E402
from app.web.security import token_hash  # noqa: E402
from fakes import FakePool, repository_with_pool  # noqa: E402
from fixtures import make_plan, make_recipe_row, make_shopping_item  # noqa: E402


HOUSEHOLD = uuid.uuid4()
USER = uuid.uuid4()
SESSION = {
    "household_id": HOUSEHOLD,
    "user_id": USER,
    "login": "hozyain",
    "role": "owner",
    "csrf_hash": "хеш",
    "household_name": "Моя семья",
}


def session(**overrides):
    return {**SESSION, **overrides}


class CsrfTests(unittest.TestCase):
    """S1 — сравнение CSRF-токена должно быть constant-time."""

    def test_s1_csrf_valid_uses_compare_digest(self) -> None:
        token = "токен"
        current = {"csrf_hash": token_hash(token)}
        with mock.patch.object(
            db_module.secrets, "compare_digest", wraps=secrets.compare_digest
        ) as spy:
            self.assertTrue(AppRepository.csrf_valid(current, token))
        spy.assert_called_once()

    def test_s1_csrf_valid_rejects_missing_and_wrong_token(self) -> None:
        current = {"csrf_hash": token_hash("правильный")}
        self.assertFalse(AppRepository.csrf_valid(current, None))
        self.assertFalse(AppRepository.csrf_valid(current, ""))
        self.assertFalse(AppRepository.csrf_valid(current, "неправильный"))
        self.assertFalse(AppRepository.csrf_valid({"csrf_hash": ""}, "правильный"))


class AffectedRowsTests(unittest.TestCase):
    """S6 — «DELETE 11» не должно считаться успешным удалением одной строки."""

    def test_s6_affected_rows_parses_command_tag(self) -> None:
        self.assertEqual(db_module.affected_rows("DELETE 1"), 1)
        self.assertEqual(db_module.affected_rows("DELETE 11"), 11)
        self.assertEqual(db_module.affected_rows("DELETE 0"), 0)
        self.assertEqual(db_module.affected_rows("UPDATE 3"), 3)
        self.assertEqual(db_module.affected_rows(None), 0)
        self.assertEqual(db_module.affected_rows("МУСОР"), 0)

    def test_s6_delete_inventory_true_only_for_exactly_one_row(self) -> None:
        pool = FakePool()
        pool.default_execute = "DELETE 11"
        repository = repository_with_pool(pool)
        deleted = self._run(repository.delete_inventory(session(), uuid.uuid4()))
        self.assertFalse(deleted)

    def test_s6_delete_inventory_true_for_one_row(self) -> None:
        pool = FakePool()
        pool.default_execute = "DELETE 1"
        repository = repository_with_pool(pool)
        self.assertTrue(self._run(repository.delete_inventory(session(), uuid.uuid4())))

    def test_s6_delete_inventory_is_household_scoped(self) -> None:
        pool = FakePool()
        pool.default_execute = "DELETE 1"
        repository = repository_with_pool(pool)
        self._run(repository.delete_inventory(session(), uuid.uuid4()))
        sql, args = pool.first_matching("DELETE FROM app_core.inventory_lots")
        self.assertIn("household_id=$2", sql)
        self.assertEqual(args[1], HOUSEHOLD)

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)


class LatestPlanTests(unittest.TestCase):
    """B1/A1 — сохранённый план обязан возвращаться всегда."""

    def _pool_with_plan(self, meal_title: str) -> FakePool:
        pool = FakePool()
        header = make_plan()
        header.pop("meals")
        header.pop("shopping")
        header.pop("warnings")
        pool.on("fetchrow", "FROM app_core.meal_plans", header)
        pool.on("fetch", "FROM app_core.plan_meals", [{
            "meal_date": "2026-08-17",
            "meal_type": "lunch",
            "recipe_id": 42,
            "scale": Decimal("1"),
            "servings": Decimal("2"),
            "estimated_kcal": 520,
            "title": meal_title,
            "cuisine_code": None,
            "review_status": "ready",
            "source_page_start": 12,
        }])
        pool.on("fetch", "FROM app_core.plan_ingredients", [make_shopping_item()])
        return pool

    def _latest(self, meal_title: str):
        import asyncio

        pool = self._pool_with_plan(meal_title)
        repository = repository_with_pool(pool)
        return asyncio.run(repository.latest_plan(session()))

    def test_b1_a1_latest_plan_survives_bad_dish_title(self) -> None:
        plan = self._latest("СОДЕРЖАНИЕ")
        self.assertIsNotNone(plan, "план с «плохим» названием блюда пропал после перезагрузки")
        self.assertEqual(len(plan["meals"]), 1)
        self.assertEqual(len(plan["shopping"]), 1)

    def test_b1_a1_latest_plan_still_cleans_titles(self) -> None:
        plan = self._latest("ПЛОВ С БАРАНИНОЙ Глава 3")
        self.assertNotIn("Глава", plan["meals"][0]["title"])

    def test_b1_a1_latest_plan_returns_none_without_header(self) -> None:
        import asyncio

        repository = repository_with_pool(FakePool())
        self.assertIsNone(asyncio.run(repository.latest_plan(session())))


def run_async(coro):
    import asyncio

    return asyncio.run(coro)


class ListRecipesTests(unittest.TestCase):
    """B7/A3 — отбор и подсчёт делает SQL, ингредиенты доезжают до карточки."""

    def _list(self, rows, **kwargs):
        pool = FakePool()
        pool.on("fetch", "FROM recipe_library.recipes", rows)
        repository = repository_with_pool(pool)
        return pool, run_async(repository.list_recipes(**kwargs))

    def test_b7_a3_list_recipes_uses_sql_limit_offset(self) -> None:
        pool, _ = self._list([], limit=24, offset=48)
        sql, args = pool.first_matching("FROM recipe_library.recipes")
        self.assertIn("LIMIT $5 OFFSET $6", sql)
        self.assertIn("count(*) OVER ()", sql)
        self.assertEqual(args[4], 24)
        self.assertEqual(args[5], 48)

    def test_b7_a3_list_recipes_orders_stably_for_pagination(self) -> None:
        pool, _ = self._list([])
        sql, _ = pool.first_matching("FROM recipe_library.recipes")
        # Первый ORDER BY — внутри подзапроса имён ингредиентов, нужен последний.
        order = sql.rsplit("ORDER BY", 1)[1].split("LIMIT", 1)[0]
        self.assertIn("r.id", order, "без стабильного ключа OFFSET теряет строки")
        self.assertNotIn(
            "r.title", order,
            "сортировка по названию в C.UTF-8 выносит латиницу впереди кириллицы",
        )

    def test_b7_a3_list_recipes_does_not_filter_in_python(self) -> None:
        rows = [
            make_recipe_row(id=1, title="Плов с бараниной", total_count=3),
            make_recipe_row(id=2, title="СОДЕРЖАНИЕ", total_count=3),
            make_recipe_row(id=3, title="Recipe with latin words inside", total_count=3),
        ]
        _, page = self._list(rows)
        self.assertEqual(len(page["items"]), 3, "строки отсеялись в Python вместо SQL")

    def test_b7_a3_list_recipes_reports_total_and_has_more(self) -> None:
        rows = [make_recipe_row(id=index, total_count=120) for index in range(1, 25)]
        _, page = self._list(rows, limit=24, offset=0)
        self.assertEqual(page["total"], 120)
        self.assertTrue(page["has_more"])
        _, last = self._list(
            [make_recipe_row(id=index, total_count=25) for index in range(1, 2)],
            limit=24, offset=24,
        )
        self.assertFalse(last["has_more"])

    def test_b7_a3_empty_page_reports_zero_total(self) -> None:
        _, page = self._list([])
        self.assertEqual(page["total"], 0)
        self.assertFalse(page["has_more"])

    def test_a3_list_recipes_returns_ingredient_names(self) -> None:
        _, page = self._list([make_recipe_row(ingredient_names=["рис", "баранина", ""])])
        self.assertEqual(page["items"][0]["ingredient_names"], ["рис", "баранина"])

    def test_a3_list_recipes_limits_ingredient_names_in_sql(self) -> None:
        pool, _ = self._list([])
        sql, _ = pool.first_matching("FROM recipe_library.recipes")
        self.assertIn("ORDER BY i.position LIMIT 6", sql)

    def test_a3_list_recipes_passes_ready_only_flag(self) -> None:
        pool, _ = self._list([], ready_only=True)
        sql, args = pool.first_matching("FROM recipe_library.recipes")
        self.assertIn("$4 = FALSE OR r.review_status = 'ready'", sql)
        self.assertIs(args[3], True)

    def test_a3_list_recipes_parses_jsonb_columns(self) -> None:
        _, page = self._list([make_recipe_row(meal_types='["lunch", "dinner"]')])
        self.assertEqual(page["items"][0]["meal_types"], ["lunch", "dinner"])

    def test_a3_list_recipes_clamps_limit_and_offset(self) -> None:
        pool, _ = self._list([], limit=5000, offset=-10)
        _, args = pool.first_matching("FROM recipe_library.recipes")
        self.assertEqual(args[4], 100)
        self.assertEqual(args[5], 0)


class RecipeDetailTests(unittest.TestCase):
    """S5 + A4 — не отдавать источник, отдавать section/note/is_to_taste."""

    def _detail(self, *, title="Плов с бараниной", ingredient_overrides=None):
        from fixtures import make_ingredient

        pool = FakePool()
        pool.on("fetchrow", "FROM recipe_library.recipes", {
            "id": 42, "title": title, "source_page_start": 12, "source_page_end": 13,
            "source_servings_min": None, "source_servings_max": None,
            "source_yield_text": None, "cuisine_code": None, "meal_types": "[]",
            "diet_tags": "[]", "appliances": "[]", "review_status": "needs_review",
            "review_reasons": "[]", "ingredient_count": 1, "step_count": 1,
            "time_total_minutes": None, "extraction_confidence": Decimal("0.9"),
        })
        pool.on("fetch", "FROM recipe_library.recipe_ingredients",
                [make_ingredient(**(ingredient_overrides or {}))])
        pool.on("fetch", "FROM recipe_library.recipe_steps",
                [{"position": 1, "instruction": "Смешать и запечь"}])
        repository = repository_with_pool(pool)
        return pool, run_async(repository.recipe_detail(42))

    def test_s5_recipe_detail_selects_explicit_columns(self) -> None:
        pool, detail = self._detail()
        sql, _ = pool.first_matching("FROM recipe_library.recipes")
        self.assertNotIn("r.*", sql)
        for leaked in ("source_id", "fingerprint", "raw_text", "cuisine_confidence"):
            self.assertNotIn(leaked, detail, f"наружу утекло поле {leaked}")

    def test_a4_recipe_detail_returns_section_note_and_is_to_taste(self) -> None:
        _, detail = self._detail(ingredient_overrides={
            "section": "для соуса", "note": "промыть", "is_to_taste": True,
        })
        ingredient = detail["ingredients"][0]
        self.assertEqual(ingredient["section"], "для соуса")
        self.assertEqual(ingredient["note"], "промыть")
        self.assertTrue(ingredient["is_to_taste"])

    def test_a3_recipe_detail_opens_recipe_that_list_shows(self) -> None:
        """Фильтры качества сняты: видимый в списке рецепт обязан открываться."""
        _, detail = self._detail(title="СОДЕРЖАНИЕ")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["id"], 42)

    def test_a3_recipe_detail_returns_none_for_missing_recipe(self) -> None:
        repository = repository_with_pool(FakePool())
        self.assertIsNone(run_async(repository.recipe_detail(999)))


class PlannerDataTests(unittest.TestCase):
    """B5/A5 — планировщик не тянет всю библиотеку."""

    def _planner_data(self):
        pool = FakePool()
        repository = repository_with_pool(pool)
        return pool, run_async(repository.planner_data(session()))

    def test_b5_a5_planner_data_uses_ready_only_and_limit(self) -> None:
        pool, _ = self._planner_data()
        sql, args = pool.first_matching("FROM recipe_library.recipes r")
        self.assertIn("r.review_status = 'ready'", sql)
        self.assertIn("LIMIT $1", sql)
        self.assertEqual(args[0], db_module.PLANNER_RECIPE_LIMIT)

    def test_b5_a5_planner_data_passes_matcher_to_build_plan(self) -> None:
        _, data = self._planner_data()
        self.assertIn("product_matcher", data)
        self.assertEqual(data["products"], data["product_matcher"].products)

    def test_b5_a5_planner_data_parses_ingredient_json(self) -> None:
        pool = FakePool()
        pool.on("fetch", "FROM recipe_library.recipes r", [{
            "id": 1, "title": "Блюдо", "source_page_start": 1,
            "source_servings_min": None, "cuisine_code": None, "meal_types": ["lunch"],
            "appliances": [], "review_status": "ready",
            "extraction_confidence": Decimal("0.9"),
            "ingredients": '[{"ingredient_text": "молоко", "normalized_name": "молоко"}]',
        }])
        repository = repository_with_pool(pool)
        data = run_async(repository.planner_data(session()))
        self.assertEqual(data["recipes"][0]["ingredients"][0]["normalized_name"], "молоко")


class CuisinePoolTests(unittest.TestCase):
    """Выбранная кухня должна попадать в пул, а не теряться в топ-500."""

    def test_pool_adds_window_for_selected_cuisines(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.planner_recipe_pool(["asian"]))
        sql, args = pool.first_matching("FROM recipe_library.recipes r")
        self.assertIn("cuisine_code = ANY", sql)
        self.assertIn(["asian"], args)

    def test_planner_data_passes_cuisines_to_the_pool(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.planner_data(session(), cuisines=["asian", "japanese"]))
        _, args = pool.first_matching("FROM recipe_library.recipes r")
        self.assertIn(["asian", "japanese"], args)


class MealReplacementTests(unittest.TestCase):
    """Замена блюда предлагает десяток вариантов, а не три."""

    PLAN_ID = uuid.uuid4()
    MEAL_ID = uuid.uuid4()

    @staticmethod
    def _recipe(recipe_id: int) -> dict:
        return {
            "id": recipe_id,
            "title": f"Дневное блюдо {recipe_id}",
            "source_page_start": recipe_id,
            "source_servings_min": Decimal("2"),
            "cuisine_code": "russian",
            "meal_types": ["lunch"],
            "appliances": [],
            "review_status": "ready",
            "extraction_confidence": Decimal("0.9"),
            "dish_type": "soup",
            "ingredients": '[{"ingredient_text": "молоко", "normalized_name": "молоко",'
                           ' "unit_code": "ml", "quantity_min": 200, "quantity_max": 200}]',
        }

    def _alternatives(self) -> list[dict]:
        pool = FakePool()
        pool.on("fetchrow", "FROM app_core.meal_plans", {
            "id": self.PLAN_ID, "price_tier": "balanced", "cuisine_preferences": "[]",
        })
        pool.on("fetch", "FROM app_core.plan_meals", [{
            "id": self.MEAL_ID, "meal_date": date(2026, 8, 20), "meal_type": "lunch",
            "recipe_id": 1, "servings": Decimal("2"),
        }])
        pool.on("fetch", "FROM recipe_library.recipes r", [
            self._recipe(recipe_id) for recipe_id in range(1, 26)
        ])
        repository = repository_with_pool(pool)
        result = run_async(repository.replace_meal(session(), self.PLAN_ID, self.MEAL_ID))
        return result["alternatives"]

    def test_replacement_offers_ten_alternatives(self) -> None:
        self.assertEqual(len(self._alternatives()), db_module.MEAL_ALTERNATIVES_LIMIT)
        self.assertEqual(db_module.MEAL_ALTERNATIVES_LIMIT, 10)


class PlannerWarmUpTests(unittest.TestCase):
    """N1 — «Составить меню» подвисало на десятки секунд с холодными кэшами."""

    RECIPE_ROW = {
        "id": 1, "title": "Блюдо", "source_page_start": 1,
        "source_servings_min": None, "cuisine_code": None, "meal_types": ["lunch"],
        "appliances": [], "review_status": "ready",
        "extraction_confidence": Decimal("0.9"),
        "ingredients": '[{"ingredient_text": "молоко", "normalized_name": "молоко",'
                       ' "unit_code": "ml", "quantity_min": 200, "quantity_max": 200}]',
    }

    def _warm(self):
        pool = FakePool()
        pool.on("fetch", "FROM recipe_library.recipes r", [dict(self.RECIPE_ROW)])
        repository = repository_with_pool(pool)
        return pool, repository

    def test_n1_warm_up_touches_recipe_pool_and_marks_matcher(self) -> None:
        pool, repository = self._warm()
        warmed = run_async(repository.warm_planner_caches())
        self.assertEqual(warmed, 1, "прогрет должен быть каждый ингредиент пула")
        pool.first_matching("FROM recipe_library.recipes r")
        matcher = run_async(repository.product_matcher())
        self.assertTrue(matcher.warmed)

    def test_n1_warm_up_is_skipped_for_already_warm_matcher(self) -> None:
        _, repository = self._warm()
        run_async(repository.warm_planner_caches())
        self.assertEqual(run_async(repository.warm_planner_caches()), 0)

    def test_n1_planner_data_reuses_the_same_recipe_pool_query(self) -> None:
        """Прогрев обязан греть ровно тот запрос, который потом делает план."""
        pool, repository = self._warm()
        run_async(repository.warm_planner_caches())
        warm_sql, warm_args = pool.first_matching("FROM recipe_library.recipes r")
        pool.calls.clear()
        run_async(repository.planner_data(session()))
        plan_sql, plan_args = pool.first_matching("FROM recipe_library.recipes r")
        self.assertEqual(warm_sql, plan_sql)
        self.assertEqual(warm_args, plan_args)


class DashboardTests(unittest.TestCase):
    """B6/A5 — без коррелированных regex-подзапросов."""

    def test_b6_a5_dashboard_query_has_no_regex_subqueries(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "recipe_library.recipes", {"recipes": 3130, "recipes_ready": 988})
        repository = repository_with_pool(pool)
        run_async(repository.dashboard(session()))
        sql, _ = pool.first_matching("recipe_library.recipes")
        self.assertNotIn("NOT EXISTS", sql)
        self.assertNotIn(" ~ ", sql)
        self.assertIn("recipes_ready", sql)


class AuthenticateTests(unittest.TestCase):
    """B8/A5 — last_seen_at пишется не чаще раза в 5 минут."""

    def _authenticate(self, *, stale: bool):
        pool = FakePool()
        pool.on("fetchrow", "FROM app_core.user_sessions s", {
            "user_id": USER, "login": "hozyain", "csrf_hash": "хеш",
            "household_id": HOUSEHOLD, "household_name": "Моя семья",
            "role": "owner", "last_seen_stale": stale,
        })
        repository = repository_with_pool(pool)
        return pool, run_async(repository.authenticate("токен-сессии"))

    def test_b8_a5_authenticate_skips_update_when_fresh(self) -> None:
        pool, current = self._authenticate(stale=False)
        self.assertIsNotNone(current)
        self.assertEqual(pool.count_matching("UPDATE app_core.user_sessions"), 0)
        self.assertEqual(len(pool.calls), 1)

    def test_b8_a5_authenticate_updates_when_stale(self) -> None:
        pool, _ = self._authenticate(stale=True)
        sql, _ = pool.first_matching("UPDATE app_core.user_sessions")
        self.assertIn("INTERVAL '5 minutes'", sql)

    def test_b8_a5_authenticate_hashes_token_once(self) -> None:
        with mock.patch.object(db_module, "token_hash", wraps=db_module.token_hash) as spy:
            self._authenticate(stale=True)
        self.assertEqual(spy.call_count, 1)


class ProductMatcherCacheUsageTests(unittest.TestCase):
    """B4/A5 — каталог не сканируется на каждое открытие рецепта."""

    def test_b4_a5_two_recipe_details_scan_catalogue_once(self) -> None:
        pool = FakePool()
        for recipe_id in (1, 2):
            pool.on("fetchrow", "FROM recipe_library.recipes", {
                "id": recipe_id, "title": "Плов", "source_page_start": 1,
                "source_page_end": 1, "source_servings_min": None,
                "source_servings_max": None, "source_yield_text": None,
                "cuisine_code": None, "meal_types": "[]", "diet_tags": "[]",
                "appliances": "[]", "review_status": "ready", "review_reasons": "[]",
                "ingredient_count": 0, "step_count": 0, "time_total_minutes": None,
                "extraction_confidence": Decimal("0.9"),
            })
        repository = repository_with_pool(pool)
        self.assertIsNotNone(run_async(repository.recipe_detail(1)))
        self.assertIsNotNone(run_async(repository.recipe_detail(2)))
        self.assertEqual(pool.count_matching("FROM lenta_store.store_products p"), 1)

    def test_b4_a5_catalogue_stamp_is_index_only(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.catalogue_stamp())
        sql, args = pool.first_matching("max(observed_on)")
        self.assertIn("store_price_history", sql)
        self.assertEqual(args[0], db_module.STORE_CODE)


class PlanEndpointDataTests(unittest.TestCase):
    """A4 — история, удаление и отметка «куплено» на уровне SQL."""

    def test_a4_shopping_list_includes_item_id_and_purchased_at(self) -> None:
        pool = FakePool()
        header = make_plan()
        for key in ("meals", "shopping", "warnings"):
            header.pop(key)
        pool.on("fetchrow", "FROM app_core.meal_plans", header)
        repository = repository_with_pool(pool)
        run_async(repository.latest_plan(session()))
        sql, _ = pool.first_matching("FROM app_core.plan_ingredients")
        self.assertIn("pi.id", sql)
        self.assertIn("pi.purchased_at", sql)
        self.assertIn("category_slug", sql)

    def test_a4_get_plan_is_household_scoped(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        plan_id = uuid.uuid4()
        self.assertIsNone(run_async(repository.get_plan(session(), plan_id)))
        sql, args = pool.first_matching("FROM app_core.meal_plans WHERE id=$1")
        self.assertIn("household_id=$2", sql)
        self.assertEqual(args, (plan_id, HOUSEHOLD))

    def test_a4_delete_plan_uses_affected_rows(self) -> None:
        pool = FakePool()
        pool.default_execute = "DELETE 11"
        repository = repository_with_pool(pool)
        self.assertFalse(run_async(repository.delete_plan(session(), uuid.uuid4())))

    def test_a4_delete_plan_forbidden_for_viewer(self) -> None:
        repository = repository_with_pool(FakePool())
        with self.assertRaises(PermissionError):
            run_async(repository.delete_plan(session(role="viewer"), uuid.uuid4()))

    def test_a4_mark_purchased_sql_is_household_scoped(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        plan_id, item_id = uuid.uuid4(), uuid.uuid4()
        run_async(repository.mark_purchased(session(), plan_id, item_id, True))
        sql, args = pool.first_matching("UPDATE app_core.plan_ingredients")
        self.assertIn("mp.household_id=$3", sql)
        self.assertEqual(args, (item_id, plan_id, HOUSEHOLD, True))

    def test_a4_list_plans_counts_meals(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.list_plans(session()))
        sql, args = pool.first_matching("FROM app_core.meal_plans p")
        self.assertIn("AS meal_count", sql)
        self.assertEqual(args[1], 20)


class SettingsPeopleTests(unittest.TestCase):
    """A4 — люди сохраняют свои id между сохранениями настроек."""

    def _save(self, people):
        pool = FakePool()
        pool.on("fetch", "SELECT id FROM app_core.people", [{"id": self.existing_id}])
        repository = repository_with_pool(pool)
        run_async(repository.save_settings(session(), "Моя семья", people, [], []))
        return pool

    def setUp(self) -> None:
        self.existing_id = uuid.uuid4()

    def test_a4_save_settings_updates_people_in_place(self) -> None:
        pool = self._save([{"id": self.existing_id, "name": "Ваня"}])
        sql, args = pool.first_matching("UPDATE app_core.people")
        self.assertIn("WHERE id=$1 AND household_id=$2", sql)
        self.assertEqual(args[0], self.existing_id)
        self.assertEqual(pool.count_matching("INSERT INTO app_core.people"), 0)

    def test_a4_save_settings_inserts_person_without_id(self) -> None:
        pool = self._save([{"id": self.existing_id, "name": "Ваня"}, {"name": "Маша"}])
        self.assertEqual(pool.count_matching("INSERT INTO app_core.people"), 1)

    def test_a4_save_settings_removes_person_absent_from_payload(self) -> None:
        pool = self._save([{"name": "Маша"}])
        sql, args = pool.first_matching("DELETE FROM app_core.people")
        self.assertIn("id <> ALL($2::uuid[])", sql)
        self.assertNotIn(self.existing_id, args[1])

    def test_a4_update_person_builds_only_provided_columns(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.update_person(session(), self.existing_id, {"name": "Ваня"}))
        sql, args = pool.first_matching("UPDATE app_core.people SET")
        assignments = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        self.assertEqual(assignments, "name = $3")
        self.assertEqual(args[2], "Ваня")

    def test_a4_update_person_can_clear_target_kcal(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.update_person(session(), self.existing_id, {"target_kcal": None}))
        sql, args = pool.first_matching("UPDATE app_core.people SET")
        self.assertIn("target_kcal = $3", sql)
        self.assertIsNone(args[2])

    def test_a4_update_person_without_changes_is_value_error(self) -> None:
        repository = repository_with_pool(FakePool())
        with self.assertRaises(ValueError):
            run_async(repository.update_person(session(), self.existing_id, {}))

    def test_a4_update_person_forbidden_for_editor(self) -> None:
        repository = repository_with_pool(FakePool())
        with self.assertRaises(PermissionError):
            run_async(repository.update_person(session(role="editor"), self.existing_id, {"name": "Х"}))


class ReviewStatusTests(unittest.TestCase):
    """A4 — очередь проверки рецептов."""

    def test_a4_set_review_status_rejects_unknown_value(self) -> None:
        repository = repository_with_pool(FakePool())
        with self.assertRaises(ValueError):
            run_async(repository.set_review_status(session(), 1, "готов"))

    def test_a4_set_review_status_requires_owner_or_admin(self) -> None:
        repository = repository_with_pool(FakePool())
        for role in ("editor", "viewer"):
            with self.assertRaises(PermissionError):
                run_async(repository.set_review_status(session(role=role), 1, "ready"))

    def test_a4_set_review_status_updates_and_audits(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "UPDATE recipe_library.recipes",
                {"id": 1, "title": "Плов", "review_status": "ready"})
        repository = repository_with_pool(pool)
        result = run_async(repository.set_review_status(session(), 1, "ready"))
        self.assertEqual(result["review_status"], "ready")
        self.assertEqual(pool.count_matching("INSERT INTO app_core.audit_log"), 1)

    def test_a4_set_review_status_missing_recipe_returns_none(self) -> None:
        repository = repository_with_pool(FakePool())
        self.assertIsNone(run_async(repository.set_review_status(session(), 999, "ready")))


class ListProductsTests(unittest.TestCase):
    """Пагинация и фильтр скидок для «Ленты» (часть Б2)."""

    def test_products_page_reports_total(self) -> None:
        pool = FakePool()
        pool.on("fetch", "FROM lenta_store.store_products p", [
            {"id": 1, "name": "Молоко", "total_count": 13190},
        ])
        repository = repository_with_pool(pool)
        page = run_async(repository.list_products(limit=1, offset=0))
        self.assertEqual(page["total"], 13190)
        self.assertTrue(page["has_more"])
        self.assertNotIn("total_count", page["items"][0])

    def test_products_discount_filter_is_passed_to_sql(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.list_products(discount_only=True))
        sql, args = pool.first_matching("FROM lenta_store.store_products p")
        self.assertIn("ph.promo_price_kop < ph.regular_price_kop", sql)
        self.assertIs(args[1], True)

    def test_products_discount_filter_ignores_loyalty_discount(self) -> None:
        """discount_percent считается от цены по Карте №1 — он стоит почти у всех."""
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.list_products(discount_only=True))
        sql, _ = pool.first_matching("FROM lenta_store.store_products p")
        condition = sql.split("$2 = FALSE OR", 1)[1].split("ORDER BY", 1)[0]
        self.assertNotIn("discount_percent", condition)

    def test_products_default_sort_is_alphabetical(self) -> None:
        """«Сначала дешёвые» пугала стеной стиков по 15 ₽ — дефолт по алфавиту."""
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.list_products())
        sql, _ = pool.first_matching("FROM lenta_store.store_products p")
        order_clause = sql.rsplit("ORDER BY", 1)[1]
        self.assertIn("p.name", order_clause.split("LIMIT", 1)[0])
        self.assertNotIn("effective_price_kop", order_clause)

    def test_products_category_filter_is_passed_to_sql(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.list_products(category="molochnye-produkty-yajjco-3"))
        sql, args = pool.first_matching("FROM lenta_store.store_products p")
        self.assertIn("store_product_categories", sql)
        self.assertIn("molochnye-produkty-yajjco-3", args)


class RowDictTests(unittest.TestCase):
    def test_row_dict_converts_uuid_date_and_decimal(self) -> None:
        converted = row_dict({
            "id": HOUSEHOLD,
            "quantity": Decimal("1.5"),
            "name": "Молоко",
        })
        self.assertEqual(converted["id"], str(HOUSEHOLD))
        self.assertEqual(converted["quantity"], 1.5)
        self.assertEqual(converted["name"], "Молоко")


class TelegramAccountSqlTests(unittest.TestCase):
    """TZ-M7 §3.2–3.4: аккаунт без пароля, код входа, отвязка."""

    def test_account_from_bot_has_no_password_row(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.register_account("tg7", None, "Моя семья", telegram_user_id=7))
        self.assertEqual(pool.count_matching("INSERT INTO app_core.password_credentials"), 0)
        # привязка идёт в той же транзакции, что и создание аккаунта
        _, args = pool.first_matching("INSERT INTO app_core.auth_identities")
        self.assertEqual(args[0], "7")

    def test_account_from_web_keeps_password_row(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.register_account("hozyain", "parol12345", "Моя семья"))
        self.assertEqual(pool.count_matching("INSERT INTO app_core.password_credentials"), 1)
        self.assertEqual(pool.count_matching("INSERT INTO app_core.auth_identities"), 0)

    def test_registration_from_bot_is_audited_as_telegram(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        run_async(repository.register_account(
            "tg7", None, "Моя семья", telegram_user_id=7, channel="telegram"
        ))
        _, args = pool.first_matching("INSERT INTO app_core.audit_log")
        self.assertIn("telegram", args)

    def test_web_login_code_is_six_digits_and_short_lived(self) -> None:
        pool = FakePool()
        repository = repository_with_pool(pool)
        code = run_async(repository.web_login_code(USER))
        self.assertRegex(code, r"^\d{6}$")
        sql, args = pool.first_matching("INSERT INTO app_core.one_time_tokens")
        self.assertIn("'web_login'", sql)
        # в базе лежит хеш, а не сам код
        self.assertNotIn(code, [str(arg) for arg in args])

    def test_login_code_is_burned_and_checked_for_expiry(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "UPDATE app_core.one_time_tokens", {"user_id": USER})
        pool.on("fetchval", "SELECT status FROM app_core.users", "active")
        repository = repository_with_pool(pool)
        run_async(repository.telegram_login("123456"))
        sql, _ = pool.first_matching("UPDATE app_core.one_time_tokens")
        self.assertIn("used_at IS NULL", sql)
        self.assertIn("expires_at > CURRENT_TIMESTAMP", sql)
        self.assertIn("purpose='web_login'", sql)

    def test_unknown_code_raises_authentication_error(self) -> None:
        from app.web.database import AuthenticationError

        repository = repository_with_pool(FakePool())  # fetchrow отдаёт None
        with self.assertRaises(AuthenticationError):
            run_async(repository.telegram_login("000000"))

    def test_blocked_account_cannot_enter_by_code(self) -> None:
        from app.web.database import AuthenticationError

        pool = FakePool()
        pool.on("fetchrow", "UPDATE app_core.one_time_tokens", {"user_id": USER})
        pool.on("fetchval", "SELECT status FROM app_core.users", "blocked")
        with self.assertRaises(AuthenticationError):
            run_async(repository_with_pool(pool).telegram_login("123456"))

    def test_set_password_upserts(self) -> None:
        pool = FakePool()
        run_async(repository_with_pool(pool).set_password(session(), "novyyparol"))
        sql, _ = pool.first_matching("INSERT INTO app_core.password_credentials")
        self.assertIn("ON CONFLICT (user_id) DO UPDATE", sql)

    def test_unlink_clears_dialog_state(self) -> None:
        pool = FakePool()
        pool.on("fetchrow", "DELETE FROM app_core.auth_identities", {"provider_user_id": "88112250"})
        self.assertTrue(run_async(repository_with_pool(pool).unlink_telegram(session())))
        _, args = pool.first_matching("DELETE FROM app_core.telegram_dialog_state")
        self.assertEqual(args, (88112250,))

    def test_unlink_without_link_returns_false(self) -> None:
        pool = FakePool()  # fetchrow отдаёт None
        self.assertFalse(run_async(repository_with_pool(pool).unlink_telegram(session())))
        self.assertEqual(pool.count_matching("DELETE FROM app_core.telegram_dialog_state"), 0)

    def test_profile_reports_password_presence(self) -> None:
        pool = FakePool()
        pool.on("fetchval", "FROM app_core.password_credentials", 1)
        profile = run_async(repository_with_pool(pool).get_profile(session()))
        self.assertTrue(profile["user"]["has_password"])


if __name__ == "__main__":
    unittest.main()
