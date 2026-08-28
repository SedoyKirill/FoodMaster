"""HTTP-слой ``app/web/main.py`` через TestClient с подменённым репозиторием.

TZ-TESTS §3.3. Проверяется обвязка: коды ответов, CSRF, роли, изоляция household,
форма JSON. Логика SQL живёт в ``test_web_database.py``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from fastapi.testclient import TestClient

    from app.web import main
    from app.web import planner as planner_mod
    from app.web.main import create_app
except ImportError as exc:  # pragma: no cover - окружение без fastapi/httpx
    raise unittest.SkipTest(f"веб-стек недоступен: {exc}") from exc

from app.web.planner import DEFAULT_APPLIANCES  # noqa: E402
from fakes import FakeRepository, expires_in, make_client  # noqa: E402


class AppFactoryTests(unittest.TestCase):
    def test_create_app_injects_repository(self) -> None:
        repository = FakeRepository()
        with TestClient(create_app(repository)) as client:
            self.assertEqual(client.get("/health").status_code, 200)
        self.assertIs(create_app(repository).state.repository, repository)

    def test_create_app_returns_independent_instances(self) -> None:
        first = create_app(FakeRepository())
        second = create_app(FakeRepository())
        self.assertIsNot(first.state.repository, second.state.repository)


class StaticShellTests(unittest.TestCase):
    """N1 — 404 на /favicon.ico маскировал настоящие ошибки в консоли."""

    def test_favicon_is_served(self) -> None:
        with TestClient(create_app(FakeRepository())) as client:
            response = client.get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/"))

    def test_index_declares_icon(self) -> None:
        with TestClient(create_app(FakeRepository())) as client:
            page = client.get("/").text
        self.assertIn('rel="icon"', page)


class PlanCuisineTests(unittest.TestCase):
    """Кухни из формы должны доехать до пула рецептов, иначе фильтровать нечего."""

    def test_generate_passes_selected_cuisines_to_planner_data(self) -> None:
        client, repository = make_client(self)
        client.post(
            "/api/plans/generate",
            json={"starts_on": "2026-08-20", "days": 3, "cuisines": ["asian"],
                  "price_tier": "balanced"},
        )
        self.assertEqual(repository.planner_data_cuisines, ["asian"])


class PlannerWarmUpTests(unittest.TestCase):
    """N1 — сборка меню не должна платить за холодные кэши в момент клика."""

    def test_startup_warms_planner_caches(self) -> None:
        repository = FakeRepository()
        with TestClient(create_app(repository)) as client:
            client.get("/health")
        self.assertGreaterEqual(repository.warm_calls, 1)


class AuthTests(unittest.TestCase):
    def test_register_sets_session_and_csrf_cookies(self) -> None:
        client, _ = make_client(self)
        self.assertIn("ration_session", client.cookies)
        self.assertIn("ration_csrf", client.cookies)
        self.assertEqual(client.get("/api/me").json()["user"]["login"], "hozyain")

    def test_register_duplicate_login_is_409(self) -> None:
        client, repository = make_client(self)
        response = client.post(
            "/api/auth/register",
            json={"login": "hozyain", "password": "parol12345", "household_name": "Другая"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(repository.users), 1)

    def test_protected_endpoint_without_session_is_401(self) -> None:
        with TestClient(create_app(FakeRepository())) as client:
            self.assertEqual(client.get("/api/recipes").status_code, 401)
            self.assertEqual(client.get("/api/inventory").status_code, 401)
            self.assertEqual(client.get("/api/me").status_code, 401)


class CsrfTests(unittest.TestCase):
    def test_mutation_without_csrf_header_is_403(self) -> None:
        client, _ = make_client(self)
        del client.headers["X-CSRF-Token"]
        response = client.post(
            "/api/inventory",
            json={"name": "Молоко", "quantity": 1, "unit_code": "l"},
        )
        self.assertEqual(response.status_code, 403)

    def test_mutation_with_wrong_csrf_is_403(self) -> None:
        client, _ = make_client(self)
        client.headers["X-CSRF-Token"] = "wrong-token-value"
        response = client.post(
            "/api/inventory",
            json={"name": "Молоко", "quantity": 1, "unit_code": "l"},
        )
        self.assertEqual(response.status_code, 403)

    def test_mutation_with_valid_csrf_succeeds(self) -> None:
        client, _ = make_client(self)
        response = client.post(
            "/api/inventory",
            json={"name": "Молоко", "quantity": 1, "unit_code": "l"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["name"], "Молоко")


class RoleTests(unittest.TestCase):
    def test_viewer_cannot_add_inventory(self) -> None:
        client, _ = make_client(self, role="viewer")
        response = client.post(
            "/api/inventory",
            json={"name": "Молоко", "quantity": 1, "unit_code": "l"},
        )
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_generate_plan(self) -> None:
        client, _ = make_client(self, role="viewer")
        self.assertEqual(client.post("/api/plans/generate", json={"days": 3}).status_code, 403)

    def test_editor_cannot_change_settings(self) -> None:
        client, _ = make_client(self, role="editor")
        response = client.put(
            "/api/settings",
            json={"household_name": "Семья", "people": [{"name": "Я"}]},
        )
        self.assertEqual(response.status_code, 403)


class HouseholdIsolationTests(unittest.TestCase):
    """TZ-M1 §критерий: горизонтальное повышение прав и перебор household_id."""

    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.first, _ = make_client(self, repository=self.repository, login="pervyy")
        self.second, _ = make_client(self, repository=self.repository, login="vtoroy")

    def test_inventory_is_not_shared_between_households(self) -> None:
        created = self.first.post(
            "/api/inventory",
            json={"name": "Молоко", "quantity": 1, "unit_code": "l"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(len(self.first.get("/api/inventory").json()), 1)
        self.assertEqual(self.second.get("/api/inventory").json(), [])

    def test_deleting_foreign_inventory_is_404_not_403(self) -> None:
        item_id = self.first.post(
            "/api/inventory",
            json={"name": "Молоко", "quantity": 1, "unit_code": "l"},
        ).json()["id"]
        response = self.second.delete(f"/api/inventory/{item_id}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.first.get("/api/inventory").json()), 1)


class SecurityHeaderTests(unittest.TestCase):
    """S4 — CSP и спутники должны стоять на любом ответе."""

    def _assert_headers(self, response) -> None:
        self.assertTrue(
            response.headers["content-security-policy"].startswith("default-src 'self'"),
            response.headers.get("content-security-policy"),
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "same-origin")

    def test_s4_security_headers_present_on_api(self) -> None:
        client, _ = make_client(self)
        self._assert_headers(client.get("/api/me"))

    def test_s4_security_headers_present_on_health(self) -> None:
        with TestClient(create_app(FakeRepository())) as client:
            self._assert_headers(client.get("/health"))

    def test_font_is_served_with_font_mime_type(self) -> None:
        """С nosniff шрифт с типом text/plain браузер отвергнет."""
        with TestClient(create_app(FakeRepository())) as client:
            response = client.get("/assets/fonts/InterVariable.woff2")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "font/woff2")
            self.assertIn("immutable", response.headers["cache-control"])

    def test_s4_security_headers_present_on_error(self) -> None:
        with TestClient(create_app(FakeRepository())) as client:
            response = client.get("/api/inventory")
            self.assertEqual(response.status_code, 401)
            self._assert_headers(response)


class LoginRateLimitTests(unittest.TestCase):
    """S2 — Argon2 стоит 19 МиБ на попытку, перебор нужно останавливать."""

    def test_s2_sixth_login_attempt_returns_429_with_retry_after(self) -> None:
        client, _ = make_client(self)
        payload = {"login": "hozyain", "password": "невернейпароль"}
        for attempt in range(5):
            self.assertEqual(client.post("/api/auth/login", json=payload).status_code, 401, attempt)
        blocked = client.post("/api/auth/login", json=payload)
        self.assertEqual(blocked.status_code, 429)
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)

    def test_s2_successful_login_clears_the_bucket(self) -> None:
        client, _ = make_client(self)
        good = {"login": "hozyain", "password": "parol12345"}
        bad = {"login": "hozyain", "password": "неверный"}
        for _ in range(4):
            client.post("/api/auth/login", json=bad)
        self.assertEqual(client.post("/api/auth/login", json=good).status_code, 200)
        for _ in range(5):
            self.assertNotEqual(client.post("/api/auth/login", json=bad).status_code, 429)

    def test_s2_limiters_are_per_application(self) -> None:
        first, repository = make_client(self, login="pervyy")
        for _ in range(6):
            first.post("/api/auth/login", json={"login": "pervyy", "password": "неверный"})
        second, _ = make_client(self, repository=repository, login="vtoroy")
        self.assertEqual(
            second.post("/api/auth/login", json={"login": "vtoroy", "password": "parol12345"}).status_code,
            200,
        )


class LogoutTests(unittest.TestCase):
    """S3 — потеряв CSRF-куку, пользователь всё равно должен уметь выйти."""

    def test_s3_logout_without_csrf_header_succeeds(self) -> None:
        client, _ = make_client(self)
        del client.headers["X-CSRF-Token"]
        self.assertEqual(client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_s3_logout_without_session_is_401(self) -> None:
        with TestClient(create_app(FakeRepository())) as client:
            self.assertEqual(client.post("/api/auth/logout").status_code, 401)


class InventoryValidationTests(unittest.TestCase):
    """S8 — срок годности в прошлом портит FEFO-сортировку."""

    def _add(self, client, **overrides):
        payload = {"name": "Молоко", "quantity": 1, "unit_code": "l"}
        payload.update(overrides)
        return client.post("/api/inventory", json=payload)

    def test_s8_past_expires_on_is_rejected(self) -> None:
        client, _ = make_client(self)
        response = self._add(client, expires_on=expires_in(-1))
        self.assertEqual(response.status_code, 422)
        self.assertIn("просрочено", response.json()["detail"])

    def test_s8_past_expires_on_accepted_with_flag(self) -> None:
        client, _ = make_client(self)
        self.assertEqual(
            self._add(client, expires_on=expires_in(-1), already_expired=True).status_code, 201
        )

    def test_s8_today_and_future_expires_on_accepted(self) -> None:
        client, _ = make_client(self)
        self.assertEqual(self._add(client, expires_on=expires_in(0)).status_code, 201)
        self.assertEqual(self._add(client, expires_on=expires_in(5)).status_code, 201)

    def test_s8_already_expired_flag_is_not_stored(self) -> None:
        client, _ = make_client(self)
        created = self._add(client, expires_on=expires_in(-2), already_expired=True).json()
        self.assertNotIn("already_expired", created)

    def test_unknown_unit_and_storage_are_422(self) -> None:
        client, _ = make_client(self)
        self.assertEqual(self._add(client, unit_code="ведро").status_code, 422)
        self.assertEqual(self._add(client, storage_area="балкон").status_code, 422)


class PlanGenerationTests(unittest.TestCase):
    """Новый пользователь обязан получить меню сразу после регистрации.

    Раньше это обеспечивалось отключением фильтра техники (A2), теперь —
    базовым набором техники у новой семьи (TZ-M8 §3.3).
    """

    def test_new_user_gets_default_appliances_and_can_generate_plan(self) -> None:
        client, repository = make_client(self)
        self.assertEqual(
            sorted(repository.appliances[next(iter(repository.households))]),
            sorted(DEFAULT_APPLIANCES),
        )
        response = client.post("/api/plans/generate", json={"days": 3, "starts_on": "2026-08-17"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(response.json()["meals"]), 9)

    def test_b1_a1_generated_plan_is_returned_after_reload(self) -> None:
        client, _ = make_client(self)
        created = client.post("/api/plans/generate", json={"days": 3, "starts_on": "2026-08-17"})
        self.assertEqual(created.status_code, 201, created.text)
        latest = client.get("/api/plans/latest")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["id"], created.json()["id"])
        self.assertEqual(len(latest.json()["meals"]), 9)


def seed_recipes(repository: FakeRepository) -> None:
    repository.recipes.update({
        1: {"id": 1, "title": "Плов с бараниной", "review_status": "ready",
            "cuisine_code": None, "meal_types": ["lunch", "dinner"],
            "ingredient_names": ["рис", "баранина", "морковь"], "ingredient_count": 3,
            "source_page_start": 12, "source_page_end": 13, "steps": []},
        2: {"id": 2, "title": "Блины на молоке", "review_status": "needs_review",
            "cuisine_code": None, "meal_types": ["breakfast"],
            "ingredient_names": ["молоко", "мука"], "ingredient_count": 2,
            "source_page_start": 3, "source_page_end": 3, "steps": []},
    })


class RecipeListTests(unittest.TestCase):
    """A3 — список отдаёт ингредиенты, счётчик и признак «есть ещё»."""

    def setUp(self) -> None:
        self.client, self.repository = make_client(self)
        seed_recipes(self.repository)

    def test_a3_recipes_response_has_items_and_total(self) -> None:
        page = self.client.get("/api/recipes").json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 2)
        self.assertFalse(page["has_more"])

    def test_a3_recipes_items_carry_ingredient_names_and_status(self) -> None:
        first = self.client.get("/api/recipes").json()["items"][0]
        self.assertIn("рис", first["ingredient_names"])
        self.assertIn(first["review_status"], {"ready", "needs_review"})

    def test_a3_recipes_pagination_reports_has_more(self) -> None:
        page = self.client.get("/api/recipes?limit=1").json()
        self.assertEqual(len(page["items"]), 1)
        self.assertTrue(page["has_more"])
        second = self.client.get("/api/recipes?limit=1&offset=1").json()
        self.assertFalse(second["has_more"])

    def test_a3_ready_only_filters_drafts_out(self) -> None:
        page = self.client.get("/api/recipes?ready_only=true").json()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["review_status"], "ready")

    def test_a3_facets_are_served_before_recipe_id_route(self) -> None:
        response = self.client.get("/api/recipes/facets")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cuisines"], [], "кухни в библиотеке не размечены")
        self.assertIn("lunch", response.json()["meal_types"])

    def test_s5_recipe_detail_hides_source_identifiers(self) -> None:
        detail = self.client.get("/api/recipes/1").json()
        for leaked in ("source_id", "fingerprint", "source_title", "raw_text"):
            self.assertNotIn(leaked, detail)


class RecipeReviewTests(unittest.TestCase):
    """A4 — очередь проверки: смена статуса из детали рецепта."""

    def test_a4_owner_can_mark_recipe_ready(self) -> None:
        client, repository = make_client(self)
        seed_recipes(repository)
        response = client.post("/api/recipes/2/review", json={"status": "ready"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["review_status"], "ready")
        self.assertEqual(repository.recipes[2]["review_status"], "ready")

    def test_a4_review_is_reversible(self) -> None:
        client, repository = make_client(self)
        seed_recipes(repository)
        client.post("/api/recipes/1/review", json={"status": "rejected"})
        client.post("/api/recipes/1/review", json={"status": "needs_review"})
        self.assertEqual(repository.recipes[1]["review_status"], "needs_review")

    def test_a4_review_forbidden_for_editor_and_viewer(self) -> None:
        for role in ("editor", "viewer"):
            with self.subTest(role=role):
                client, repository = make_client(self, role=role)
                seed_recipes(repository)
                response = client.post("/api/recipes/1/review", json={"status": "ready"})
                self.assertEqual(response.status_code, 403)

    def test_a4_review_unknown_status_is_422(self) -> None:
        client, repository = make_client(self)
        seed_recipes(repository)
        self.assertEqual(
            client.post("/api/recipes/1/review", json={"status": "готов"}).status_code, 422
        )

    def test_a4_review_missing_recipe_is_404(self) -> None:
        client, _ = make_client(self)
        self.assertEqual(
            client.post("/api/recipes/999/review", json={"status": "ready"}).status_code, 404
        )

    def test_a4_review_requires_csrf(self) -> None:
        client, repository = make_client(self)
        seed_recipes(repository)
        del client.headers["X-CSRF-Token"]
        self.assertEqual(
            client.post("/api/recipes/1/review", json={"status": "ready"}).status_code, 403
        )


class PlanHistoryTests(unittest.TestCase):
    """A4 — история планов, чтение по id, удаление, отметка «куплено»."""

    def setUp(self) -> None:
        self.client, self.repository = make_client(self)
        created = self.client.post(
            "/api/plans/generate", json={"days": 3, "starts_on": "2026-08-17"}
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.plan_id = created.json()["id"]

    def test_a4_plans_history_returns_saved_plans(self) -> None:
        items = self.client.get("/api/plans").json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], self.plan_id)
        self.assertEqual(items[0]["meal_count"], 9)

    def test_a4_get_plan_by_id(self) -> None:
        plan = self.client.get(f"/api/plans/{self.plan_id}").json()
        self.assertEqual(plan["id"], self.plan_id)
        self.assertEqual(len(plan["meals"]), 9)

    def test_a4_plans_latest_is_routed_before_plan_id(self) -> None:
        response = self.client.get("/api/plans/latest")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], self.plan_id)

    def test_a4_get_other_household_plan_is_404(self) -> None:
        other, _ = make_client(self, repository=self.repository, login="sosed")
        self.assertEqual(other.get(f"/api/plans/{self.plan_id}").status_code, 404)
        self.assertEqual(other.get("/api/plans").json()["items"], [])

    def test_a4_delete_plan_removes_it(self) -> None:
        self.assertEqual(self.client.delete(f"/api/plans/{self.plan_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/plans/{self.plan_id}").status_code, 404)

    def test_a4_delete_other_household_plan_is_404(self) -> None:
        other, _ = make_client(self, repository=self.repository, login="sosed")
        self.assertEqual(other.delete(f"/api/plans/{self.plan_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/plans/{self.plan_id}").status_code, 200)

    def test_a4_delete_plan_forbidden_for_viewer(self) -> None:
        for household in self.repository.households.values():
            household["role"] = "viewer"
        self.assertEqual(self.client.delete(f"/api/plans/{self.plan_id}").status_code, 403)

    def test_a4_mark_item_purchased_persists(self) -> None:
        plan = self.client.get(f"/api/plans/{self.plan_id}").json()
        item_id = plan["shopping"][0]["id"]
        response = self.client.patch(
            f"/api/plans/{self.plan_id}/items/{item_id}", json={"purchased": True}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()["purchased_at"])
        reloaded = self.client.get(f"/api/plans/{self.plan_id}").json()
        marked = next(item for item in reloaded["shopping"] if item["id"] == item_id)
        self.assertIsNotNone(marked["purchased_at"])

    def test_a4_unmark_item_purchased(self) -> None:
        plan = self.client.get(f"/api/plans/{self.plan_id}").json()
        item_id = plan["shopping"][0]["id"]
        self.client.patch(f"/api/plans/{self.plan_id}/items/{item_id}", json={"purchased": True})
        response = self.client.patch(
            f"/api/plans/{self.plan_id}/items/{item_id}", json={"purchased": False}
        )
        self.assertIsNone(response.json()["purchased_at"])

    def test_a4_mark_purchased_in_other_household_is_404(self) -> None:
        plan = self.client.get(f"/api/plans/{self.plan_id}").json()
        item_id = plan["shopping"][0]["id"]
        other, _ = make_client(self, repository=self.repository, login="sosed")
        response = other.patch(
            f"/api/plans/{self.plan_id}/items/{item_id}", json={"purchased": True}
        )
        self.assertEqual(response.status_code, 404)

    def test_a4_mark_unknown_item_is_404(self) -> None:
        import uuid as uuid_module

        response = self.client.patch(
            f"/api/plans/{self.plan_id}/items/{uuid_module.uuid4()}", json={"purchased": True}
        )
        self.assertEqual(response.status_code, 404)


class TasteApiTests(unittest.TestCase):
    """События вкуса, онбординг и сводка (TZ-M8 §4)."""

    def setUp(self) -> None:
        self.client, self.repository = make_client(self)
        created = self.client.post(
            "/api/plans/generate", json={"days": 1, "starts_on": "2026-08-17"}
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.plan = self.client.get("/api/plans/latest").json()

    def test_marking_a_meal_cooked_records_a_taste_event(self) -> None:
        meal = self.plan["meals"][0]
        response = self.client.patch(
            f"/api/plans/{self.plan['id']}/meals/{meal['id']}", json={"status": "cooked"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cooked")
        kinds = [event["kind"] for event in self.repository.taste_events_rows]
        self.assertIn("cooked", kinds)

    def test_unknown_status_is_rejected(self) -> None:
        meal = self.plan["meals"][0]
        response = self.client.patch(
            f"/api/plans/{self.plan['id']}/meals/{meal['id']}", json={"status": "съели"}
        )
        self.assertEqual(response.status_code, 422)

    def test_status_of_a_stranger_meal_is_404(self) -> None:
        import uuid as uuid_module

        response = self.client.patch(
            f"/api/plans/{self.plan['id']}/meals/{uuid_module.uuid4()}",
            json={"status": "skipped"},
        )
        self.assertEqual(response.status_code, 404)

    def test_onboarding_is_offered_to_a_family_without_history(self) -> None:
        body = self.client.get("/api/taste/onboarding").json()
        self.assertTrue(body["needed"])
        self.assertTrue(body["cards"])

    def test_onboarding_answers_become_events(self) -> None:
        cards = self.client.get("/api/taste/onboarding").json()["cards"]
        answers = [
            {"recipe_id": cards[0]["recipe_id"], "liked": True},
            {"recipe_id": cards[1]["recipe_id"], "liked": False},
            {"recipe_id": cards[2]["recipe_id"], "liked": None},  # пропуск
        ]
        response = self.client.post("/api/taste/onboarding", json={"answers": answers})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["saved"], 2)
        kinds = [event["kind"] for event in self.repository.taste_events_rows]
        self.assertIn("onboarding_like", kinds)
        self.assertIn("onboarding_skip", kinds)

    def test_summary_reports_what_the_family_likes(self) -> None:
        cards = self.client.get("/api/taste/onboarding").json()["cards"]
        liked = cards[0]["recipe_id"]
        self.client.post(
            "/api/taste/onboarding",
            json={"answers": [{"recipe_id": liked, "liked": True}]},
        )
        summary = self.client.get("/api/taste/summary").json()
        self.assertEqual(summary["favourite_recipes"][0]["recipe_id"], liked)

    def test_viewer_cannot_answer_onboarding(self) -> None:
        for household in self.repository.households.values():
            household["role"] = "viewer"
        response = self.client.post(
            "/api/taste/onboarding", json={"answers": [{"recipe_id": 1, "liked": True}]}
        )
        self.assertEqual(response.status_code, 403)


class PlanProfileApiTests(unittest.TestCase):
    """Профиль планирования семьи (TZ-M8 §3.4)."""

    def setUp(self) -> None:
        self.client, self.repository = make_client(self)

    def test_defaults_are_returned_before_first_save(self) -> None:
        profile = self.client.get("/api/settings/plan-profile").json()
        self.assertEqual(profile["mode"], "balanced")
        self.assertEqual(profile["default_days"], 7)
        # Решение владельца: кухня остаётся жёстким фильтром по умолчанию.
        self.assertEqual(profile["cuisine_mode"], "only")

    def test_saved_profile_prefills_generation(self) -> None:
        """План без полей формы собирается по профилю: дни и приёмы оттуда."""
        saved = self.client.put("/api/settings/plan-profile", json={
            "mode": "quick", "default_days": 2, "meals": ["breakfast", "dinner"],
            "cuisines": [], "cuisine_mode": "only",
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.client.post("/api/plans/generate", json={"starts_on": "2026-08-17"})
        self.assertEqual(response.status_code, 201, response.text)
        plan = response.json()
        self.assertEqual(plan["days"], 2)
        self.assertEqual(plan["mode"], "quick")
        self.assertEqual(
            sorted({meal["meal_type"] for meal in plan["meals"]}), ["breakfast", "dinner"]
        )

    def test_form_overrides_profile_without_saving_it(self) -> None:
        self.client.put("/api/settings/plan-profile", json={"default_days": 5})
        response = self.client.post(
            "/api/plans/generate", json={"days": 1, "starts_on": "2026-08-17"}
        )
        self.assertEqual(response.json()["days"], 1)
        self.assertEqual(
            self.client.get("/api/settings/plan-profile").json()["default_days"], 5
        )

    def test_mode_from_the_form_reaches_the_planner(self) -> None:
        """Режим — это веса целевой функции (§6.4), а не строчка в ответе.

        Перехват стоит на самом планировщике, а не на обработчике: сборку
        плана зовёт слой данных, и тот же путь проходит бот (TZ-M7 §2).
        """
        seen: dict[str, object] = {}
        original = planner_mod.build_plan

        def _capture(**kwargs):
            seen.update(kwargs)
            return original(**kwargs)

        with mock.patch.object(planner_mod, "build_plan", _capture):
            response = self.client.post(
                "/api/plans/generate",
                json={"days": 1, "starts_on": "2026-08-17", "mode": "economy"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(seen["mode"], "economy")
        # economy заодно переключает ценовую стратегию матчера товаров.
        self.assertEqual(seen["price_tier"], "economy")

    def test_weekly_budget_scales_to_horizon(self) -> None:
        self.client.put("/api/settings/plan-profile", json={"weekly_budget_kop": 700000})
        response = self.client.post(
            "/api/plans/generate", json={"days": 1, "starts_on": "2026-08-17"}
        )
        self.assertEqual(response.json()["budget_kop"], 100000)

    def test_two_week_horizon_is_accepted(self) -> None:
        response = self.client.post(
            "/api/plans/generate", json={"days": 14, "starts_on": "2026-08-17"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["days"], 14)

    def test_horizon_over_two_weeks_is_rejected(self) -> None:
        response = self.client.post(
            "/api/plans/generate", json={"days": 15, "starts_on": "2026-08-17"}
        )
        self.assertEqual(response.status_code, 422)

    def test_editor_cannot_save_plan_profile(self) -> None:
        for household in self.repository.households.values():
            household["role"] = "editor"
        response = self.client.put("/api/settings/plan-profile", json={"mode": "economy"})
        self.assertEqual(response.status_code, 403)


class SettingsPeopleApiTests(unittest.TestCase):
    """A4 — люди не пересоздаются, редактируются по одному."""

    def setUp(self) -> None:
        self.client, self.repository = make_client(self)

    def _people(self):
        return self.client.get("/api/me").json()["people"]

    def test_a4_put_settings_keeps_person_ids(self) -> None:
        before = self._people()
        payload = {
            "household_name": "Моя семья",
            "people": [{"id": before[0]["id"], "name": "Иван"}],
        }
        self.assertEqual(self.client.put("/api/settings", json=payload).status_code, 200)
        after = self._people()
        self.assertEqual(after[0]["id"], before[0]["id"])
        self.assertEqual(after[0]["name"], "Иван")

    def test_a4_patch_person_updates_single_field(self) -> None:
        person_id = self._people()[0]["id"]
        response = self.client.patch(
            f"/api/settings/people/{person_id}", json={"target_kcal": 2100}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["target_kcal"], 2100)

    def test_person_profile_fields_survive_save(self) -> None:
        """TZ-M8 §3.1: мерки, цель и «ест дома» доходят до хранилища."""
        person_id = self._people()[0]["id"]
        payload = {
            "household_name": "Моя семья",
            "people": [{
                "id": person_id, "name": "Иван", "sex": "male",
                "birth_date": "1990-05-01", "height_cm": 180, "weight_kg": 80,
                "activity": "high", "goal": "lose",
                "eats_meals": ["breakfast", "dinner"],
            }],
        }
        self.assertEqual(self.client.put("/api/settings", json=payload).status_code, 200)
        saved = self._people()[0]
        self.assertEqual(saved["sex"], "male")
        self.assertEqual(saved["goal"], "lose")
        self.assertEqual(saved["eats_meals"], ["breakfast", "dinner"])

    def test_person_target_shows_how_it_was_calculated(self) -> None:
        """Норма едока приходит с пометкой источника (manual/formula/default)."""
        person_id = self._people()[0]["id"]
        self.client.patch(f"/api/settings/people/{person_id}", json={"target_kcal": 2100})
        response = self.client.get(f"/api/settings/people/{person_id}/target")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["kcal"], 2100)
        self.assertEqual(body["target_source"], "manual")
        self.assertEqual(sum(body["by_meal"].values()), 2100)

    def test_person_target_404_for_stranger(self) -> None:
        import uuid as uuid_module

        response = self.client.get(f"/api/settings/people/{uuid_module.uuid4()}/target")
        self.assertEqual(response.status_code, 404)

    def test_rule_can_belong_to_one_person(self) -> None:
        """TZ-M8 §3.2: правило с person_id и diet_tag сохраняется как есть."""
        person_id = self._people()[0]["id"]
        payload = {
            "household_name": "Моя семья",
            "people": [{"id": person_id, "name": "Иван"}],
            "dietary_rules": [{
                "rule_type": "allergy", "term": "орехи", "is_hard": True,
                "person_id": person_id, "diet_tag": "vegetarian",
            }],
        }
        self.assertEqual(self.client.put("/api/settings", json=payload).status_code, 200)
        rule = self.client.get("/api/me").json()["dietary_rules"][0]
        self.assertEqual(str(rule["person_id"]), person_id)
        self.assertEqual(rule["diet_tag"], "vegetarian")

    def test_a4_patch_person_forbidden_for_editor(self) -> None:
        person_id = self._people()[0]["id"]
        for household in self.repository.households.values():
            household["role"] = "editor"
        response = self.client.patch(f"/api/settings/people/{person_id}", json={"name": "Ы"})
        self.assertEqual(response.status_code, 403)

    def test_a4_patch_unknown_person_is_404(self) -> None:
        import uuid as uuid_module

        response = self.client.patch(
            f"/api/settings/people/{uuid_module.uuid4()}", json={"name": "Ы"}
        )
        self.assertEqual(response.status_code, 404)

    def test_a4_patch_person_without_fields_is_422(self) -> None:
        person_id = self._people()[0]["id"]
        self.assertEqual(
            self.client.patch(f"/api/settings/people/{person_id}", json={}).status_code, 422
        )


class ProductListTests(unittest.TestCase):
    def test_products_response_is_paginated(self) -> None:
        client, repository = make_client(self)
        repository.products.extend([
            {"id": index, "name": f"Товар {index}", "promo_price_kop": None}
            for index in range(1, 6)
        ])
        page = client.get("/api/products?limit=2").json()
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])


class TelegramAccountTests(unittest.TestCase):
    """TZ-M7 §3.3–3.4: вход по коду из бота, пароль постфактум, отвязка."""

    def test_code_from_bot_opens_a_session(self) -> None:
        repository = FakeRepository()
        make_client(self, repository=repository)
        code = asyncio.run(repository.web_login_code(next(iter(repository.users))))

        fresh = TestClient(create_app(repository))
        self.assertEqual(fresh.get("/api/me").status_code, 401)
        response = fresh.post("/api/auth/telegram-login", json={"code": code})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fresh.get("/api/me").status_code, 200)

    def test_code_works_only_once(self) -> None:
        repository = FakeRepository()
        make_client(self, repository=repository)
        code = asyncio.run(repository.web_login_code(next(iter(repository.users))))
        client = TestClient(create_app(repository))
        self.assertEqual(client.post("/api/auth/telegram-login", json={"code": code}).status_code, 200)
        second = TestClient(create_app(repository)).post(
            "/api/auth/telegram-login", json={"code": code}
        )
        self.assertEqual(second.status_code, 401)

    def test_wrong_code_is_401_not_500(self) -> None:
        client = TestClient(create_app(FakeRepository()))
        self.assertEqual(
            client.post("/api/auth/telegram-login", json={"code": "000000"}).status_code, 401
        )

    def test_code_guessing_is_rate_limited(self) -> None:
        """Код всего шестизначный — лимит и есть основная защита от перебора."""
        client = TestClient(create_app(FakeRepository()))
        codes = [client.post("/api/auth/telegram-login", json={"code": f"{i:06d}"})
                 for i in range(12)]
        statuses = [response.status_code for response in codes]
        self.assertIn(429, statuses)
        limited = codes[statuses.index(429)]
        self.assertTrue(limited.headers.get("Retry-After"))

    def test_set_password_requires_csrf(self) -> None:
        client, _ = make_client(self)
        del client.headers["X-CSRF-Token"]
        self.assertEqual(
            client.post("/api/auth/set-password", json={"password": "новыйпароль"}).status_code,
            403,
        )

    def test_set_password_is_recorded(self) -> None:
        client, repository = make_client(self)
        self.assertTrue(client.get("/api/me").json()["user"]["has_password"])
        response = client.post("/api/auth/set-password", json={"password": "другойпароль"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("set_password", [name for name, _ in repository.calls])

    def test_short_password_is_422(self) -> None:
        client, _ = make_client(self)
        response = client.post("/api/auth/set-password", json={"password": "1234"})
        self.assertEqual(response.status_code, 422)

    def test_unlink_removes_link(self) -> None:
        client, repository = make_client(self)
        user_id = client.get("/api/me").json()["user"]["id"]
        repository.telegram_links["88112250"] = user_id
        self.assertTrue(client.get("/api/me").json()["telegram_linked"])
        self.assertEqual(client.delete("/api/telegram/link").status_code, 200)
        self.assertFalse(client.get("/api/me").json()["telegram_linked"])

    def test_unlink_without_link_is_404(self) -> None:
        client, _ = make_client(self)
        self.assertEqual(client.delete("/api/telegram/link").status_code, 404)

    def test_unlink_requires_csrf(self) -> None:
        client, _ = make_client(self)
        del client.headers["X-CSRF-Token"]
        self.assertEqual(client.delete("/api/telegram/link").status_code, 403)


if __name__ == "__main__":
    unittest.main()
