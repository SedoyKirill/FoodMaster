"""Тестовые двойники слоя данных (TZ-TESTS §4).

Два разных инструмента для двух разных слоёв — не путать:

* ``FakePool`` подменяет ``asyncpg.Pool``/``Connection`` и проверяет **SQL и логику
  репозитория**: какой запрос ушёл, с какими параметрами, сколько раз.
* ``FakeRepository`` подменяет ``AppRepository`` целиком и проверяет **HTTP-обвязку**:
  коды ответов, права, CSRF, изоляцию household, форму JSON.

Тест на баг вроде B1 («план исчезает после перезагрузки») обязан быть на ``FakePool``:
против ``FakeRepository`` он проверял бы сам себя и был бы пустым.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixtures import make_person, make_recipe  # noqa: E402


def normalize_sql(sql: str) -> str:
    """Схлопывает переносы и отступы, чтобы искать фрагменты по подстроке."""
    return re.sub(r"\s+", " ", str(sql)).strip()


class _AsyncNull:
    """Асинхронный контекст-менеджер, отдающий заранее заданное значение."""

    def __init__(self, value: Any = None) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class FakePool:
    """Пул и соединение в одном объекте: журналирует SQL, отдаёт заданные строки.

    Строки — обычные словари: ``row_dict`` в ``app/web/database.py`` работает через
    ``dict(row)``, поэтому эмулировать ``asyncpg.Record`` не нужно.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self._rules: list[tuple[str, str, Any]] = []
        self.default_execute = "UPDATE 1"

    # --- настройка ---------------------------------------------------------

    def on(self, method: str, sql_fragment: str, result: Any) -> "FakePool":
        """Отвечать ``result`` на первый ``method``-запрос, содержащий фрагмент."""
        self._rules.append((method, normalize_sql(sql_fragment), result))
        return self

    # --- интерфейс asyncpg -------------------------------------------------

    async def fetch(self, sql: str, *args: Any) -> Any:
        return self._resolve("fetch", sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._resolve("fetchrow", sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._resolve("fetchval", sql, args)

    async def execute(self, sql: str, *args: Any) -> Any:
        return self._resolve("execute", sql, args)

    async def executemany(self, sql: str, args_seq: Any) -> Any:
        return self._resolve("executemany", sql, (tuple(args_seq),))

    def acquire(self) -> _AsyncNull:
        return _AsyncNull(self)

    def transaction(self) -> _AsyncNull:
        return _AsyncNull(None)

    # --- разбор ------------------------------------------------------------

    def _resolve(self, method: str, sql: str, args: tuple[Any, ...]) -> Any:
        normalized = normalize_sql(sql)
        self.calls.append((method, normalized, args))
        for index, (rule_method, fragment, result) in enumerate(self._rules):
            if rule_method == method and fragment in normalized:
                del self._rules[index]
                return result() if callable(result) else result
        return {
            "fetch": [],
            "fetchrow": None,
            "fetchval": None,
            "execute": self.default_execute,
            "executemany": None,
        }[method]

    # --- проверки ----------------------------------------------------------

    def sql_calls(self, method: str | None = None) -> list[str]:
        return [sql for call_method, sql, _ in self.calls if method in (None, call_method)]

    def count_matching(self, fragment: str) -> int:
        needle = normalize_sql(fragment)
        return sum(1 for _, sql, _ in self.calls if needle in sql)

    def first_matching(self, fragment: str) -> tuple[str, tuple[Any, ...]]:
        needle = normalize_sql(fragment)
        for _, sql, args in self.calls:
            if needle in sql:
                return sql, args
        raise AssertionError(f"Запрос с фрагментом {fragment!r} не выполнялся. Были: {self.sql_calls()}")


def repository_with_pool(pool: FakePool, **kwargs: Any):
    """``AppRepository``, у которого пул подменён на ``FakePool``."""
    from app.web.database import AppRepository

    repository = AppRepository(database_url="postgresql://test/test", **kwargs)
    repository.pool = pool
    return repository


class FakeRepository:
    """Хранилище в памяти для ``TestClient``. Реализует только то, что зовёт main.py."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.households: dict[str, dict[str, Any]] = {}
        self.people: dict[str, list[dict[str, Any]]] = {}
        self.appliances: dict[str, list[str]] = {}
        self.rules: dict[str, list[dict[str, Any]]] = {}
        self.inventory: dict[str, list[dict[str, Any]]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.recipes: dict[int, dict[str, Any]] = {}
        self.ratings: dict[tuple[str, int], int] = {}
        self.products: list[dict[str, Any]] = []
        self.planner_recipes: list[dict[str, Any]] = []
        self.planner_appliances: list[str] = []
        self.audit: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.warm_calls = 0
        self.planner_data_cuisines: list[str] = []
        # TZ-M7 §3.3–3.4: коды входа в веб и привязки Telegram
        self.web_login_codes: dict[str, str] = {}
        self.telegram_links: dict[str, str] = {}

    # --- служебное ---------------------------------------------------------

    async def connect(self) -> None:  # pragma: no cover - симметрия с AppRepository
        return None

    async def close(self) -> None:  # pragma: no cover
        return None

    async def warm_planner_caches(self) -> int:
        self.warm_calls += 1
        return 0

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def _new_household(self, user_id: str, name: str, role: str = "owner") -> str:
        household_id = str(uuid.uuid4())
        self.households[household_id] = {"id": household_id, "name": name, "role": role}
        self.people[household_id] = [make_person(name="Я")]
        self.appliances[household_id] = []
        self.rules[household_id] = []
        self.inventory[household_id] = []
        self.users[user_id]["household_id"] = household_id
        return household_id

    # --- аутентификация ----------------------------------------------------

    async def register(self, login: str, password: str, household_name: str) -> tuple[str, str]:
        self._record("register", login)
        if any(user["login"].casefold() == login.strip().casefold() for user in self.users.values()):
            from app.web.database import ConflictError

            raise ConflictError("Такой логин уже зарегистрирован")
        if len(password) < 8:
            raise ValueError("Пароль: от 8 до 200 символов")
        user_id = str(uuid.uuid4())
        self.users[user_id] = {"id": user_id, "login": login.strip(), "password": password}
        self._new_household(user_id, household_name.strip() or "Моя семья")
        return self._create_session(user_id)

    async def login(self, login: str, password: str) -> tuple[str, str]:
        self._record("login", login)
        from app.web.database import AuthenticationError

        for user in self.users.values():
            if user["login"].casefold() == login.strip().casefold() and user["password"] == password:
                return self._create_session(user["id"])
        raise AuthenticationError("Неверный логин или пароль")

    def _create_session(self, user_id: str) -> tuple[str, str]:
        session_token = secrets.token_urlsafe(16)
        csrf_token = secrets.token_urlsafe(16)
        self.sessions[session_token] = {"user_id": user_id, "csrf": csrf_token, "revoked": False}
        return session_token, csrf_token

    async def authenticate(self, session_token: str | None) -> dict[str, Any] | None:
        if not session_token:
            return None
        record = self.sessions.get(session_token)
        if not record or record["revoked"]:
            return None
        user = self.users[record["user_id"]]
        household = self.households[user["household_id"]]
        return {
            "user_id": user["id"],
            "login": user["login"],
            "csrf_hash": record["csrf"],
            "household_id": household["id"],
            "household_name": household["name"],
            "role": household["role"],
        }

    async def logout(self, session_token: str | None) -> None:
        self._record("logout", session_token)
        if session_token and session_token in self.sessions:
            self.sessions[session_token]["revoked"] = True

    @staticmethod
    def csrf_valid(session: dict[str, Any], csrf_token: str | None) -> bool:
        expected = str(session.get("csrf_hash") or "")
        if not csrf_token or not expected:
            return False
        return secrets.compare_digest(str(csrf_token), expected)

    # --- профиль и настройки ----------------------------------------------

    async def get_profile(self, session: dict[str, Any]) -> dict[str, Any]:
        household_id = session["household_id"]
        return {
            "user": {
                "id": session["user_id"],
                "login": session["login"],
                "has_password": bool(self.users[session["user_id"]].get("password")),
            },
            "household": {
                "id": household_id,
                "name": session["household_name"],
                "role": session["role"],
            },
            "people": self.people[household_id],
            "appliances": self.appliances[household_id],
            "dietary_rules": self.rules[household_id],
            "telegram_linked": session["user_id"] in self.telegram_links.values(),
        }

    async def save_settings(
        self,
        session: dict[str, Any],
        household_name: str,
        people: list[dict[str, Any]],
        appliances: list[str],
        rules: list[dict[str, Any]],
    ) -> None:
        self._record("save_settings", household_name)
        if session["role"] not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для изменения настроек")
        if not people:
            raise ValueError("Добавьте хотя бы одного человека")
        household_id = session["household_id"]
        self.households[household_id]["name"] = household_name
        existing = {str(person["id"]): person for person in self.people[household_id]}
        saved: list[dict[str, Any]] = []
        for person in people:
            person_id = str(person.get("id") or "")
            if person_id in existing:
                existing[person_id].update(
                    {key: value for key, value in person.items() if key != "id"}
                )
                saved.append(existing[person_id])
            else:
                saved.append(make_person(**{**person, "id": str(uuid.uuid4())}))
        self.people[household_id] = saved
        self.appliances[household_id] = sorted(set(appliances))
        self.rules[household_id] = [
            {**rule, "id": str(uuid.uuid4())} for rule in rules if str(rule.get("term") or "").strip()
        ]

    async def update_person(
        self, session: dict[str, Any], person_id: uuid.UUID, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        self._record("update_person", person_id)
        if session["role"] not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для изменения настроек")
        if not changes:
            raise ValueError("Нечего изменять")
        for person in self.people[session["household_id"]]:
            if str(person["id"]) == str(person_id):
                person.update(changes)
                return person
        return None

    # --- данные экранов ----------------------------------------------------

    async def dashboard(self, session: dict[str, Any]) -> dict[str, Any]:
        household_id = session["household_id"]
        return {
            "recipes": len(self.recipes),
            "recipes_ready": sum(1 for r in self.recipes.values() if r.get("review_status") == "ready"),
            "sources": 39,
            "products": len(self.products),
            "inventory": len(self.inventory[household_id]),
            "expiring": 0,
            "latest_plan": None,
        }

    async def list_recipes(
        self,
        search: str = "",
        cuisine: str = "",
        meal_type: str = "",
        limit: int = 48,
        offset: int = 0,
        ready_only: bool = False,
        dish_type: str = "",
    ) -> dict[str, Any]:
        self._record("list_recipes", search, cuisine, meal_type, limit, offset, ready_only)
        items = [
            recipe for recipe in self.recipes.values()
            if (not search or search.casefold() in str(recipe["title"]).casefold())
            and (not ready_only or recipe.get("review_status") == "ready")
            and (not dish_type or recipe.get("dish_type") == dish_type)
        ]
        window = items[offset:offset + limit]
        return {
            "items": window,
            "total": len(items),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(window) < len(items),
        }

    async def recipe_facets(self) -> dict[str, Any]:
        cuisines = sorted({r["cuisine_code"] for r in self.recipes.values() if r.get("cuisine_code")})
        meal_types = sorted({m for r in self.recipes.values() for m in (r.get("meal_types") or [])})
        dish_types = sorted({r["dish_type"] for r in self.recipes.values() if r.get("dish_type")})
        return {"cuisines": cuisines, "meal_types": meal_types, "dish_types": dish_types}

    async def recipe_detail(
        self, recipe_id: int, household_id: Any = None
    ) -> dict[str, Any] | None:
        self._record("recipe_detail", recipe_id)
        recipe = self.recipes.get(recipe_id)
        if not recipe or recipe.get("review_status") == "rejected":
            return None
        result = {**recipe, "steps": recipe.get("steps", [])}
        if household_id is not None:
            result["my_rating"] = self.ratings.get((str(household_id), recipe_id))
        return result

    async def set_recipe_rating(
        self, session: dict[str, Any], recipe_id: int, rating: int | None
    ) -> dict[str, Any] | None:
        self._record("set_recipe_rating", recipe_id, rating)
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет ставить оценки")
        if recipe_id not in self.recipes:
            return None
        key = (str(session["household_id"]), recipe_id)
        if rating is None:
            self.ratings.pop(key, None)
        else:
            if not 1 <= int(rating) <= 5:
                raise ValueError("Оценка — от 1 до 5")
            self.ratings[key] = int(rating)
        return {"recipe_id": recipe_id, "my_rating": rating}

    async def set_review_status(
        self, session: dict[str, Any], recipe_id: int, status_value: str
    ) -> dict[str, Any] | None:
        self._record("set_review_status", recipe_id, status_value)
        if session["role"] not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для проверки рецептов")
        if status_value not in {"ready", "rejected", "needs_review"}:
            raise ValueError("Неизвестный статус проверки")
        recipe = self.recipes.get(recipe_id)
        if not recipe:
            return None
        recipe["review_status"] = status_value
        self.audit.append({"action": "recipe.reviewed", "recipe_id": recipe_id, "status": status_value})
        return {"id": recipe_id, "title": recipe["title"], "review_status": status_value}

    async def list_products(
        self, search: str = "", sort: str = "name", limit: int = 100,
        offset: int = 0, discount_only: bool = False, category: str = "",
    ) -> dict[str, Any]:
        items = [
            product for product in self.products
            if (not search or search.casefold() in str(product["name"]).casefold())
            and (not discount_only or product.get("promo_price_kop"))
            and (not category or category in product.get("category_slugs", []))
        ]
        window = items[offset:offset + limit]
        return {
            "items": window,
            "total": len(items),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(window) < len(items),
        }

    async def product_categories(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for product in self.products:
            for slug in product.get("category_slugs", []):
                counts[slug] = counts.get(slug, 0) + 1
        return [
            {"category_slug": slug, "product_count": count}
            for slug, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    # --- запасы ------------------------------------------------------------

    async def list_inventory(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        return self.inventory[session["household_id"]]

    async def add_inventory(self, session: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        self._record("add_inventory", item.get("name"))
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять запасы")
        lot = {
            "id": str(uuid.uuid4()),
            "name": str(item["name"]).strip()[:120],
            "quantity": item["quantity"],
            "unit_code": item["unit_code"],
            "expires_on": item.get("expires_on"),
            "storage_area": item.get("storage_area", "fridge"),
            "created_at": "2026-08-17T09:00:00+03:00",
        }
        self.inventory[session["household_id"]].append(lot)
        return lot

    async def delete_inventory(self, session: dict[str, Any], item_id: uuid.UUID) -> bool:
        self._record("delete_inventory", item_id)
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять запасы")
        lots = self.inventory[session["household_id"]]
        remaining = [lot for lot in lots if str(lot["id"]) != str(item_id)]
        if len(remaining) == len(lots):
            return False
        self.inventory[session["household_id"]] = remaining
        return True

    # --- планы -------------------------------------------------------------

    async def planner_data(
        self, session: dict[str, Any], cuisines: list[str] | None = None
    ) -> dict[str, Any]:
        household_id = session["household_id"]
        self.planner_data_cuisines = list(cuisines or [])
        return {
            "people": self.people[household_id],
            "appliances": self.planner_appliances or self.appliances[household_id],
            "rules": self.rules[household_id],
            "inventory": [],
            "recipes": self.planner_recipes or [make_recipe(id=index) for index in range(1, 13)],
            "products": self.products,
        }

    async def create_plan(
        self,
        session: dict[str, Any],
        *,
        starts_on: date,
        days: int,
        budget_kop: int | None = None,
        cuisines: list[str] | None = None,
        price_tier: str = "balanced",
    ) -> dict[str, Any]:
        """Как AppRepository.create_plan: сырьё → build_plan → сохранение."""
        from app.web.database import PRICE_TIERS, UNKNOWN_TIER_TEXT
        from app.web.planner import build_plan

        if session.get("role") == "viewer":
            raise PermissionError("Режим просмотра не позволяет создавать планы")
        if price_tier not in PRICE_TIERS:
            raise ValueError(UNKNOWN_TIER_TEXT)
        cuisines = list(cuisines or [])
        data = await self.planner_data(session, cuisines)
        plan = build_plan(
            household_id=str(session["household_id"]), starts_on=starts_on, days=days,
            cuisines=cuisines, price_tier=price_tier, budget_kop=budget_kop, **data,
        )
        plan_id = await self.save_plan(
            session, starts_on, days, budget_kop, cuisines, price_tier, plan
        )
        plan["id"] = plan_id
        plan["starts_on"] = starts_on
        plan["days"] = days
        plan["budget_kop"] = budget_kop
        return plan

    async def save_plan(
        self,
        session: dict[str, Any],
        starts_on: date,
        days: int,
        budget_kop: int | None,
        cuisines: list[str],
        price_tier: str,
        plan: dict[str, Any],
    ) -> str:
        plan_id = str(uuid.uuid4())
        shopping = []
        for item in plan["shopping"]:
            shopping.append({**item, "id": str(uuid.uuid4()), "purchased_at": None,
                             "category_slug": None})
        self.plans[plan_id] = {
            "id": plan_id,
            "household_id": session["household_id"],
            "starts_on": starts_on.isoformat(),
            "days": days,
            "budget_kop": budget_kop,
            "estimated_cost_kop": plan["estimated_cost_kop"],
            "matched_cost_items": plan["matched_cost_items"],
            "total_cost_items": plan["total_cost_items"],
            "cuisine_preferences": cuisines,
            "price_tier": price_tier,
            "status": "draft",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "meals": plan["meals"],
            "shopping": shopping,
            "warnings": [],
        }
        return plan_id

    def _own_plans(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            plan for plan in self.plans.values()
            if plan["household_id"] == session["household_id"]
        ]

    @staticmethod
    def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in plan.items() if key != "household_id"}

    async def latest_plan(self, session: dict[str, Any]) -> dict[str, Any] | None:
        plans = sorted(self._own_plans(session), key=lambda plan: plan["created_at"])
        return self._public_plan(plans[-1]) if plans else None

    async def list_plans(self, session: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
        plans = sorted(self._own_plans(session), key=lambda plan: plan["created_at"], reverse=True)
        return [
            {**self._public_plan(plan), "meal_count": len(plan["meals"])}
            for plan in plans[:limit]
        ]

    async def get_plan(self, session: dict[str, Any], plan_id: uuid.UUID) -> dict[str, Any] | None:
        plan = self.plans.get(str(plan_id))
        if not plan or plan["household_id"] != session["household_id"]:
            return None
        return self._public_plan(plan)

    async def delete_plan(self, session: dict[str, Any], plan_id: uuid.UUID) -> bool:
        self._record("delete_plan", plan_id)
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет удалять планы")
        plan = self.plans.get(str(plan_id))
        if not plan or plan["household_id"] != session["household_id"]:
            return False
        del self.plans[str(plan_id)]
        return True

    async def mark_purchased(
        self, session: dict[str, Any], plan_id: uuid.UUID, item_id: uuid.UUID, purchased: bool
    ) -> dict[str, Any] | None:
        self._record("mark_purchased", plan_id, item_id, purchased)
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять список покупок")
        plan = self.plans.get(str(plan_id))
        if not plan or plan["household_id"] != session["household_id"]:
            return None
        for item in plan["shopping"]:
            if str(item["id"]) == str(item_id):
                item["purchased_at"] = "2026-08-17T10:00:00+03:00" if purchased else None
                return {
                    "id": item["id"],
                    "normalized_name": item["normalized_name"],
                    "purchased_at": item["purchased_at"],
                }
        return None

    async def telegram_link_token(self, session: dict[str, Any]) -> str:
        del session
        return secrets.token_urlsafe(16)

    # --- аккаунт из бота (TZ-M7 §3.3–3.4) ---------------------------------

    async def web_login_code(self, user_id: Any) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.web_login_codes[code] = str(user_id)
        return code

    async def telegram_login(self, code: str) -> tuple[str, str]:
        from app.web.database import AuthenticationError

        self._record("telegram_login", code)
        user_id = self.web_login_codes.pop((code or "").strip(), None)
        if user_id is None:
            raise AuthenticationError("Код не подошёл: он просрочен или уже использован")
        return self._create_session(user_id)

    async def has_password(self, user_id: Any) -> bool:
        return bool(self.users[str(user_id)].get("password"))

    async def set_password(self, session: dict[str, Any], password: str) -> None:
        self._record("set_password", session["user_id"])
        if not 8 <= len(password) <= 200:
            raise ValueError("Пароль должен содержать от 8 до 200 символов")
        self.users[str(session["user_id"])]["password"] = password

    async def unlink_telegram(self, session: dict[str, Any]) -> bool:
        self._record("unlink_telegram", session["user_id"])
        linked = [
            key for key, value in self.telegram_links.items()
            if value == str(session["user_id"])
        ]
        for key in linked:
            self.telegram_links.pop(key)
        return bool(linked)


def make_client(test_case: unittest.TestCase, *, role: str = "owner",
                repository: FakeRepository | None = None, login: str = "hozyain"):
    """Регистрирует пользователя и возвращает готовый к мутациям клиент."""
    from fastapi.testclient import TestClient

    from app.web.main import create_app

    repo = repository if repository is not None else FakeRepository()
    client = TestClient(create_app(repo))
    client.__enter__()
    test_case.addCleanup(client.__exit__, None, None, None)

    response = client.post(
        "/api/auth/register",
        json={"login": login, "password": "parol12345", "household_name": "Моя семья"},
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = client.cookies["ration_csrf"]
    if role != "owner":
        for household in repo.households.values():
            household["role"] = role
    return client, repo


def expires_in(days: int) -> str:
    """Дата через N дней в ISO — без ``datetime.now()`` в проверяемых значениях."""
    return (date.today() + timedelta(days=days)).isoformat()


class StubClock:
    """Управляемые часы для токен-бакета и кэша матчера (TZ-TESTS §2.5)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


__all__ = [
    "FakePool", "FakeRepository", "StubClock", "expires_in", "make_client",
    "normalize_sql", "repository_with_pool", "Decimal", "Callable",
]
