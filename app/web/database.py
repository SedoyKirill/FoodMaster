from __future__ import annotations

import asyncio
import functools
import json
import os
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from typing import Any

import asyncpg

from .payloads import PRICE_TIERS, UNKNOWN_TIER_TEXT
from .planning.weights import price_tier_for
from .planning.context import HISTORY_WINDOW_DAYS
from .planning.profile import MEAL_TYPES, daily_target
from .planning.taste import TasteModel, build_metas, event_value
from .planner import (
    DEFAULT_APPLIANCES, ProductMatcher, ProductMatcherCache, _base_quantity,
    build_plan, clean_dish_title, warm_product_matcher,
)
from .security import hash_password, new_token, token_hash, validate_login, verify_password


STORE_CODE = "lenta-155"
REVIEW_STATUSES = {"needs_review", "ready", "rejected"}
# Планировщику незачем тянуть всю библиотеку: он всё равно выбирает лучшие
# по уверенности извлечения (A5/B5).
PLANNER_RECIPE_LIMIT = 500
# Топ-500 по уверенности распознавания перекошен в сторону больших книг: на
# «Азиатскую» там приходилось 15 рецептов из 117. Выбранные кухни добираются
# отдельным окном, иначе фильтровать в планировщике попросту нечего.
PLANNER_CUISINE_LIMIT = 300
# Сколько альтернатив показывает «Заменить» (десять с прокруткой, TZ-фидбэк).
MEAL_ALTERNATIVES_LIMIT = 10
#: сколько событий вкуса читается для модели — года истории с запасом хватает
TASTE_EVENTS_LIMIT = 5000
#: сколько частых пар «кухня + тип блюда» показывает онбординг
ONBOARDING_PAIRS = 10
#: до скольких событий семья считается новой и ей предлагается онбординг
ONBOARDING_MIN_EVENTS = 10
PRODUCT_MATCHER_TTL_SECONDS = 600.0
# N1: раз в 5 минут планировщик «разминается» в фоне — держит горячими кэш
# Postgres и мемоизацию матчера. Иначе первая после простоя сборка меню
# платила за них десятками секунд прямо во время клика пользователя.
PLANNER_WARM_INTERVAL_SECONDS = 300.0
# TZ-M7 §3.5: откуда пришло действие. Белый список, чтобы опечатка в
# псевдосессии бота не породила третий «канал» в отчётах.
AUDIT_CHANNELS = frozenset({"web", "telegram"})
# Ключ advisory-лока на применение DDL: веб и бот могут стартовать разом.
SCHEMA_LOCK_KEY = 79_160_728
# TZ-M7 §3.3: код входа в веб живёт недолго — его диктуют вслух и набирают руками.
WEB_LOGIN_CODE_TTL = timedelta(minutes=5)


class ConflictError(Exception):
    pass


class AuthenticationError(Exception):
    pass


def _value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def row_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {key: _value(value) for key, value in dict(row).items()}


def affected_rows(status: str | None) -> int:
    """Число строк из тега команды asyncpg («DELETE 1» → 1).

    Проверять успех через ``status.endswith("1")`` нельзя: «DELETE 11» тоже
    заканчивается на единицу (S6).
    """
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


def _json_column(value: Any) -> Any:
    """JSONB без зарегистрированного кодека приходит строкой — вернуть список.

    Глобальный ``set_type_codec`` здесь не годится: его энкодер сломал бы
    ``save_plan`` и ``_audit``, которые уже передают готовый ``json.dumps``.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if value is not None else []


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


#: поля человека, которые в БД лежат как JSONB
PERSON_JSON_FIELDS = frozenset({"meal_shares", "eats_meals"})
#: словари «код → допустимые значения» для полей профиля (TZ-M8 §3.1)
PERSON_ENUMS = {
    "sex": ({"female", "male"}, None),
    "activity": ({"low", "moderate", "high"}, "moderate"),
    "goal": ({"maintain", "lose", "gain"}, "maintain"),
}
PERSON_NUMERIC_FIELDS = frozenset(
    {"height_cm", "weight_kg", "protein_share", "fat_share", "carb_share"}
)


def _coerce_person_field(field: str, value: Any) -> Any:
    if field == "name":
        return str(value or "")[:80]
    if field == "person_type":
        return "child" if value == "child" else "adult"
    if field == "portion_factor":
        return Decimal(str(value))
    if field in PERSON_ENUMS:
        allowed, fallback = PERSON_ENUMS[field]
        return str(value) if value in allowed else fallback
    if field in PERSON_NUMERIC_FIELDS:
        return None if value in (None, "") else Decimal(str(value))
    if field == "birth_date":
        if isinstance(value, date) or value is None:
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None
    if field in PERSON_JSON_FIELDS:
        return json.dumps(value, ensure_ascii=False) if value is not None else None
    return value


def _person_profile_values(person: dict[str, Any]) -> tuple[Any, ...]:
    """Поля профиля едока в порядке колонок ``app_core.people`` (TZ-M8 §3.1)."""
    values = [
        _coerce_person_field(field, person.get(field))
        for field in (
            "birth_date", "sex", "height_cm", "weight_kg", "activity", "goal",
            "protein_share", "fat_share", "carb_share", "meal_shares", "eats_meals",
        )
    ]
    if values[-1] is None:  # eats_meals NOT NULL: не указано — ест дома всё
        values[-1] = json.dumps(list(MEAL_TYPES), ensure_ascii=False)
    return tuple(values)


#: Значения профиля планирования по умолчанию (TZ-M8 §3.4). Держатся рядом с
#: репозиторием, чтобы семья без сохранённого профиля и семья со свежей
#: записью планировались одинаково.
DEFAULT_PLAN_PROFILE: dict[str, Any] = {
    "mode": "balanced",
    "default_days": 7,
    "weekly_budget_kop": None,
    "cuisines": [],
    # Кухня — жёсткий фильтр (решение владельца 28.08.2026); 'prefer' мягче.
    "cuisine_mode": "only",
    "weekday_max_minutes": 45,
    "weekend_max_minutes": None,
    "breakfast_max_minutes": 25,
    "meals": ["breakfast", "lunch", "dinner"],
    "allow_leftovers": True,
    "novelty": "medium",
    "max_repeats_per_horizon": 2,
}
#: поля профиля, у которых NULL — осмысленное значение («без лимита»)
PLAN_PROFILE_NULLABLE = frozenset(
    {"weekly_budget_kop", "weekday_max_minutes", "weekend_max_minutes", "breakfast_max_minutes"}
)


class AppRepository:
    #: Порядок важен: schema_telegram.sql ссылается на users и households.
    SCHEMA_FILES: tuple[tuple[str, str], ...] = (
        ("app.store.lenta", "schema.sql"),
        ("app.recipes", "schema.sql"),
        ("app.web", "schema.sql"),
        ("app.web", "schema_telegram.sql"),  # TZ-M7 §7
    )

    def __init__(self, database_url: str | None = None, *, channel: str = "web") -> None:
        self.database_url = database_url or os.getenv(
            "DATABASE_URL", "postgresql://ration:ration@localhost:5432/ration"
        )
        self.pool: asyncpg.Pool | None = None
        self.product_cache = ProductMatcherCache(ttl_seconds=PRODUCT_MATCHER_TTL_SECONDS)
        #: канал для audit_log: у веба 'web', у бота 'telegram' (TZ-M7 §3.5)
        self.audit_channel = channel if channel in AUDIT_CHANNELS else "web"

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=10)
        async with self.pool.acquire() as connection:
            await self.apply_schema(connection)

    @classmethod
    async def apply_schema(cls, connection: asyncpg.Connection) -> None:
        """Идемпотентный DDL под advisory-локом.

        Веб и бот стартуют одновременно (оба зависят только от db), а
        одновременный «CREATE TABLE IF NOT EXISTS» из двух сессий даёт
        unique_violation в pg_type — лок это исключает.
        """
        await connection.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_KEY)
        try:
            for package, filename in cls.SCHEMA_FILES:
                schema = files(package).joinpath(filename).read_text(encoding="utf-8")
                await connection.execute(schema)
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_KEY)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def db(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return self.pool

    async def register_account(
        self,
        login: str,
        password: str | None,
        household_name: str,
        *,
        telegram_user_id: int | None = None,
        channel: str | None = None,
    ) -> uuid.UUID:
        """Новый аккаунт вместе с семьёй; возвращает id пользователя.

        ``password=None`` — аккаунт из Telegram (TZ-M7 §3.2, А1): строки в
        ``password_credentials`` нет, войти по паролю нельзя, пока владелец не
        задаст его в вебе.

        ``telegram_user_id`` — привязать Telegram сразу, в той же транзакции:
        иначе сбой между «создали аккаунт» и «привязали» оставил бы человеку
        аккаунт, в который он не может войти ни одним способом.
        """
        normalized_login = validate_login(login)
        password_hash = hash_password(password) if password is not None else None
        user_id = uuid.uuid4()
        household_id = uuid.uuid4()
        person_id = uuid.uuid4()
        try:
            async with self.db().acquire() as connection, connection.transaction():
                await connection.execute(
                    "INSERT INTO app_core.users (id, login) VALUES ($1, $2)",
                    user_id,
                    normalized_login,
                )
                if password_hash is not None:
                    await connection.execute(
                        "INSERT INTO app_core.password_credentials (user_id, password_hash) VALUES ($1, $2)",
                        user_id,
                        password_hash,
                    )
                await connection.execute(
                    "INSERT INTO app_core.households (id, name, created_by) VALUES ($1, $2, $3)",
                    household_id,
                    household_name.strip() or "Моя семья",
                    user_id,
                )
                await connection.execute(
                    "INSERT INTO app_core.household_memberships (household_id, user_id, role) VALUES ($1, $2, 'owner')",
                    household_id,
                    user_id,
                )
                await connection.execute(
                    """
                    INSERT INTO app_core.people (id, household_id, name, person_type, portion_factor)
                    VALUES ($1, $2, 'Я', 'adult', 1)
                    """,
                    person_id,
                    household_id,
                )
                if telegram_user_id is not None:
                    await connection.execute(
                        """
                        INSERT INTO app_core.auth_identities (provider, provider_user_id, user_id)
                        VALUES ('telegram', $1, $2)
                        """,
                        str(telegram_user_id),
                        user_id,
                    )
                # TZ-M8 §3.3: фильтр по технике работает всегда, поэтому семья
                # заводится с набором, который есть почти у всех, — иначе
                # первый же план оказался бы без единого блюда.
                await connection.executemany(
                    "INSERT INTO app_core.appliances (household_id, appliance_code) VALUES ($1, $2)",
                    [(household_id, code) for code in DEFAULT_APPLIANCES],
                )
                await self._audit(
                    connection, household_id, user_id, "user.registered", "user", user_id,
                    channel=channel,
                )
        except asyncpg.UniqueViolationError as exc:
            raise ConflictError("Такой логин уже зарегистрирован") from exc
        return user_id

    async def register(self, login: str, password: str, household_name: str) -> tuple[str, str]:
        """Регистрация из веба: аккаунт с паролем и сразу сессия."""
        user_id = await self.register_account(login, password, household_name)
        return await self.create_session(user_id)

    async def login(self, login: str, password: str) -> tuple[str, str]:
        row = await self.db().fetchrow(
            """
            SELECT u.id, u.status, p.password_hash
            FROM app_core.users u
            JOIN app_core.password_credentials p ON p.user_id = u.id
            WHERE lower(u.login) = lower($1)
            """,
            login.strip(),
        )
        if not row or row["status"] != "active" or not verify_password(row["password_hash"], password):
            raise AuthenticationError("Неверный логин или пароль")
        return await self.create_session(row["id"])

    async def create_session(self, user_id: uuid.UUID) -> tuple[str, str]:
        session_token = new_token()
        csrf_token = new_token()
        await self.db().execute(
            """
            INSERT INTO app_core.user_sessions (token_hash, user_id, csrf_hash, expires_at)
            VALUES ($1, $2, $3, $4)
            """,
            token_hash(session_token),
            user_id,
            token_hash(csrf_token),
            datetime.now(UTC) + timedelta(days=30),
        )
        return session_token, csrf_token

    async def authenticate(self, session_token: str | None) -> dict[str, Any] | None:
        if not session_token:
            return None
        hashed = token_hash(session_token)
        row = await self.db().fetchrow(
            """
            SELECT u.id AS user_id, u.login, s.csrf_hash, h.id AS household_id,
                   h.name AS household_name, m.role,
                   (s.last_seen_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes') AS last_seen_stale
            FROM app_core.user_sessions s
            JOIN app_core.users u ON u.id = s.user_id
            JOIN LATERAL (
                SELECT hm.household_id, hm.role
                FROM app_core.household_memberships hm
                WHERE hm.user_id = u.id
                ORDER BY hm.created_at
                LIMIT 1
            ) m ON TRUE
            JOIN app_core.households h ON h.id = m.household_id
            WHERE s.token_hash = $1 AND s.revoked_at IS NULL
              AND s.expires_at > CURRENT_TIMESTAMP AND u.status = 'active'
            """,
            hashed,
        )
        if not row:
            return None
        # A5/B8: раньше запись last_seen_at шла на каждый аутентифицированный
        # запрос, включая любой GET. Решение о «протухании» принимает БД, и то же
        # условие продублировано в WHERE — иначе параллельные запросы гонятся.
        if row["last_seen_stale"]:
            await self.db().execute(
                """
                UPDATE app_core.user_sessions
                SET last_seen_at=CURRENT_TIMESTAMP
                WHERE token_hash=$1
                  AND last_seen_at < CURRENT_TIMESTAMP - INTERVAL '5 minutes'
                """,
                hashed,
            )
        return dict(row)

    async def logout(self, session_token: str | None) -> None:
        if session_token:
            await self.db().execute(
                "UPDATE app_core.user_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE token_hash=$1",
                token_hash(session_token),
            )

    @staticmethod
    def csrf_valid(session: dict[str, Any], csrf_token: str | None) -> bool:
        expected = str(session.get("csrf_hash") or "")
        if not csrf_token or not expected:
            return False
        return secrets.compare_digest(token_hash(csrf_token), expected)

    async def get_profile(self, session: dict[str, Any]) -> dict[str, Any]:
        household_id = session["household_id"]
        people = await self.db().fetch(
            """
            SELECT id, name, person_type, target_kcal, portion_factor,
                   birth_date, sex, height_cm, weight_kg, activity, goal,
                   protein_share, fat_share, carb_share, meal_shares, eats_meals
            FROM app_core.people WHERE household_id=$1 ORDER BY position, created_at
            """,
            household_id,
        )
        appliances = await self.db().fetch(
            "SELECT appliance_code FROM app_core.appliances WHERE household_id=$1 ORDER BY appliance_code",
            household_id,
        )
        rules = await self.db().fetch(
            """
            SELECT id, rule_type, term, is_hard, person_id, diet_tag
            FROM app_core.dietary_rules WHERE household_id=$1 ORDER BY rule_type, term
            """,
            household_id,
        )
        telegram = await self.db().fetchval(
            "SELECT provider_user_id FROM app_core.auth_identities WHERE provider='telegram' AND user_id=$1",
            session["user_id"],
        )
        # TZ-M7 §3.3: у аккаунта из бота пароля нет. Интерфейс должен предложить
        # его задать и предупредить перед отвязкой, иначе войти будет нечем.
        has_password = await self.db().fetchval(
            "SELECT 1 FROM app_core.password_credentials WHERE user_id=$1",
            session["user_id"],
        )
        return {
            "user": {
                "id": str(session["user_id"]),
                "login": session["login"],
                "has_password": has_password is not None,
            },
            "household": {
                "id": str(household_id),
                "name": session["household_name"],
                "role": session["role"],
            },
            "people": [row_dict(row) for row in people],
            "appliances": [row["appliance_code"] for row in appliances],
            "dietary_rules": [row_dict(row) for row in rules],
            "telegram_linked": telegram is not None,
        }

    async def save_settings(
        self,
        session: dict[str, Any],
        household_name: str,
        people: list[dict[str, Any]],
        appliances: list[str],
        rules: list[dict[str, Any]],
    ) -> None:
        if session["role"] not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для изменения настроек")
        household_id = session["household_id"]
        if not people:
            raise ValueError("Добавьте хотя бы одного человека")
        async with self.db().acquire() as connection, connection.transaction():
            await connection.execute(
                "UPDATE app_core.households SET name=$2, updated_at=CURRENT_TIMESTAMP WHERE id=$1",
                household_id,
                household_name.strip() or "Моя семья",
            )
            # A4: люди сверяются по id, а не пересоздаются. Раньше каждое
            # сохранение настроек выдавало всем новые UUID и обнуляло created_at.
            existing = {
                row["id"] for row in await connection.fetch(
                    "SELECT id FROM app_core.people WHERE household_id=$1", household_id
                )
            }
            keep: list[uuid.UUID] = []
            for position, person in enumerate(people, 1):
                person_id = _as_uuid(person.get("id"))
                name = str(person.get("name") or f"Человек {position}")[:80]
                person_type = "child" if person.get("person_type") == "child" else "adult"
                portion_factor = Decimal(str(
                    person.get("portion_factor") or ("0.65" if person_type == "child" else "1")
                ))
                extra = _person_profile_values(person)
                if person_id in existing:
                    await connection.execute(
                        """
                        UPDATE app_core.people
                        SET name=$3, person_type=$4, target_kcal=$5, portion_factor=$6,
                            position=$7, birth_date=$8, sex=$9, height_cm=$10,
                            weight_kg=$11, activity=$12, goal=$13, protein_share=$14,
                            fat_share=$15, carb_share=$16, meal_shares=$17::jsonb,
                            eats_meals=$18::jsonb
                        WHERE id=$1 AND household_id=$2
                        """,
                        person_id, household_id, name, person_type,
                        person.get("target_kcal"), portion_factor, position, *extra,
                    )
                else:
                    person_id = uuid.uuid4()
                    await connection.execute(
                        """
                        INSERT INTO app_core.people (
                            id, household_id, name, person_type, target_kcal,
                            portion_factor, position, birth_date, sex, height_cm,
                            weight_kg, activity, goal, protein_share, fat_share,
                            carb_share, meal_shares, eats_meals
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb,$18::jsonb)
                        """,
                        person_id, household_id, name, person_type,
                        person.get("target_kcal"), portion_factor, position, *extra,
                    )
                keep.append(person_id)
            await connection.execute(
                "DELETE FROM app_core.people WHERE household_id=$1 AND id <> ALL($2::uuid[])",
                household_id, keep,
            )
            await connection.execute("DELETE FROM app_core.appliances WHERE household_id=$1", household_id)
            await connection.executemany(
                "INSERT INTO app_core.appliances (household_id, appliance_code) VALUES ($1, $2)",
                [(household_id, code) for code in sorted(set(appliances))],
            )
            await connection.execute("DELETE FROM app_core.dietary_rules WHERE household_id=$1", household_id)
            kept_people = set(keep)
            for rule in rules:
                term = str(rule.get("term") or "").strip().casefold()
                if not term:
                    continue
                # Правило нового человека приходит без сохранённого id: пока он
                # не сохранён, правило считается семейным — терять аллергию
                # из-за порядка сохранения нельзя (TZ-M8 §3.2).
                person_ref = _as_uuid(rule.get("person_id"))
                if person_ref not in kept_people:
                    person_ref = None
                diet_tag = str(rule.get("diet_tag") or "").strip().casefold() or None
                await connection.execute(
                    """
                    INSERT INTO app_core.dietary_rules (
                        id, household_id, rule_type, term, is_hard, person_id, diet_tag
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    uuid.uuid4(),
                    household_id,
                    rule.get("rule_type") if rule.get("rule_type") in {"allergy", "intolerance", "exclude", "dislike"} else "exclude",
                    term[:100],
                    bool(rule.get("is_hard", True)),
                    person_ref,
                    diet_tag[:40] if diet_tag else None,
                )
            await self._audit(connection, household_id, session["user_id"], "settings.updated", "household", household_id)

    #: колонки, которые можно менять через PATCH — имя колонки никогда не приходит извне
    PERSON_PATCH_COLUMNS = (
        "name", "person_type", "target_kcal", "portion_factor", "birth_date",
        "sex", "height_cm", "weight_kg", "activity", "goal", "protein_share",
        "fat_share", "carb_share", "meal_shares", "eats_meals",
    )

    async def update_person(
        self, session: dict[str, Any], person_id: uuid.UUID, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Частичное изменение одного человека (A4) со стабильным id."""
        if session["role"] not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для изменения настроек")
        assignments: list[str] = []
        args: list[Any] = [person_id, session["household_id"]]
        for field in self.PERSON_PATCH_COLUMNS:
            if field not in changes:
                continue
            args.append(_coerce_person_field(field, changes[field]))
            cast = "::jsonb" if field in PERSON_JSON_FIELDS else ""
            assignments.append(f"{field} = ${len(args)}{cast}")
        if not assignments:
            raise ValueError("Нечего изменять")
        row = await self.db().fetchrow(
            f"""
            UPDATE app_core.people SET {', '.join(assignments)}
            WHERE id=$1 AND household_id=$2
            RETURNING id, name, person_type, target_kcal, portion_factor, position,
                      birth_date, sex, height_cm, weight_kg, activity, goal,
                      protein_share, fat_share, carb_share, meal_shares, eats_meals
            """,
            *args,
        )
        if not row:
            return None
        await self.audit(session, "settings.person_updated", "person", person_id)
        return row_dict(row)

    # --- точечные правки настроек (TZ-M7 §5.10) --------------------------------
    # Бот меняет по одному полю за раз, поэтому полный save_settings ему не
    # годится: он требует прислать людей, технику и правила целиком, а значит
    # любая гонка двух каналов затирала бы чужие изменения.

    def _require_admin(self, session: dict[str, Any]) -> None:
        if session.get("role") not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для изменения настроек")

    async def rename_household(self, session: dict[str, Any], name: str) -> str:
        self._require_admin(session)
        name = (name or "").strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("Название семьи: от 1 до 100 символов")
        await self.db().execute(
            "UPDATE app_core.households SET name=$2, updated_at=CURRENT_TIMESTAMP WHERE id=$1",
            session["household_id"], name,
        )
        await self.audit(session, "settings.household_renamed", "household",
                         session["household_id"], {"name": name})
        return name

    async def update_appliances(self, session: dict[str, Any], codes: list[str]) -> list[str]:
        """Полный набор техники: это тринадцать тумблеров, менять их по одному
        нечем — но людей и правила такая замена уже не задевает."""
        self._require_admin(session)
        unique = sorted({str(code).strip() for code in codes if str(code).strip()})
        async with self.db().acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM app_core.appliances WHERE household_id=$1",
                session["household_id"],
            )
            if unique:
                await connection.executemany(
                    "INSERT INTO app_core.appliances (household_id, appliance_code) VALUES ($1,$2)",
                    [(session["household_id"], code) for code in unique],
                )
            await self._audit(
                connection, session["household_id"], session["user_id"],
                "settings.appliances_updated", "household", session["household_id"],
                {"codes": unique}, channel=session.get("channel"),
            )
        return unique

    async def add_person(self, session: dict[str, Any], person: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(session)
        name = str(person.get("name") or "").strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("Имя: от 1 до 80 символов")
        person_id = uuid.uuid4()
        row = await self.db().fetchrow(
            """
            INSERT INTO app_core.people (
                id, household_id, name, person_type, target_kcal, portion_factor, position
            )
            SELECT $1, $2, $3, $4, $5, $6,
                   COALESCE(MAX(p.position), 0) + 1
            FROM app_core.people p WHERE p.household_id = $2
            RETURNING id, name, person_type, target_kcal, portion_factor, position
            """,
            person_id, session["household_id"], name,
            _coerce_person_field("person_type", person.get("person_type")),
            _coerce_person_field("target_kcal", person.get("target_kcal")),
            _coerce_person_field("portion_factor", person.get("portion_factor", 1)),
        )
        await self.audit(session, "settings.person_added", "person", person_id, {"name": name})
        return row_dict(row)

    async def delete_person(self, session: dict[str, Any], person_id: uuid.UUID) -> bool:
        """Последнего человека убрать нельзя: без едоков планировать нечего."""
        self._require_admin(session)
        async with self.db().acquire() as connection, connection.transaction():
            total = await connection.fetchval(
                "SELECT count(*) FROM app_core.people WHERE household_id=$1",
                session["household_id"],
            )
            if int(total or 0) <= 1:
                raise ValueError("В семье должен остаться хотя бы один человек")
            status = await connection.execute(
                "DELETE FROM app_core.people WHERE id=$1 AND household_id=$2",
                person_id, session["household_id"],
            )
            if affected_rows(status) != 1:
                return False
            await self._audit(
                connection, session["household_id"], session["user_id"],
                "settings.person_removed", "person", person_id,
                channel=session.get("channel"),
            )
        return True

    async def add_dietary_rule(
        self, session: dict[str, Any], rule: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_admin(session)
        term = str(rule.get("term") or "").strip()
        if not 1 <= len(term) <= 100:
            raise ValueError("Продукт: от 1 до 100 символов")
        rule_id = uuid.uuid4()
        row = await self.db().fetchrow(
            """
            INSERT INTO app_core.dietary_rules (id, household_id, rule_type, term, is_hard)
            VALUES ($1,$2,$3,$4,$5)
            RETURNING id, rule_type, term, is_hard
            """,
            rule_id, session["household_id"],
            str(rule.get("rule_type") or "exclude"), term,
            bool(rule.get("is_hard", True)),
        )
        await self.audit(session, "settings.rule_added", "dietary_rule", rule_id, {"term": term})
        return row_dict(row)

    async def delete_dietary_rule(self, session: dict[str, Any], rule_id: uuid.UUID) -> bool:
        self._require_admin(session)
        status = await self.db().execute(
            "DELETE FROM app_core.dietary_rules WHERE id=$1 AND household_id=$2",
            rule_id, session["household_id"],
        )
        if affected_rows(status) != 1:
            return False
        await self.audit(session, "settings.rule_removed", "dietary_rule", rule_id)
        return True

    async def person_target(
        self, session: dict[str, Any], person_id: uuid.UUID, on_date: date | None = None
    ) -> dict[str, Any] | None:
        """Норма едока с пометкой, как она получена (TZ-M8 §3.1).

        Пользователь должен видеть, посчитана норма по мерках, взята из его
        ручной цели или подставлена константой — иначе «2000 ккал» выглядят
        как медицинское заключение.
        """
        row = await self.db().fetchrow(
            """
            SELECT id, name, person_type, target_kcal, portion_factor, birth_date,
                   sex, height_cm, weight_kg, activity, goal, protein_share,
                   fat_share, carb_share, meal_shares, eats_meals
            FROM app_core.people WHERE id=$1 AND household_id=$2
            """,
            person_id, session["household_id"],
        )
        if not row:
            return None
        person = row_dict(row)
        target = daily_target(person, on_date or date.today())
        return {
            "person_id": str(person["id"]),
            "name": person["name"],
            "kcal": target.kcal,
            "protein_g": target.protein_g,
            "fat_g": target.fat_g,
            "carb_g": target.carb_g,
            "by_meal": target.by_meal,
            "target_source": target.source,
        }

    #: колонки профиля планирования (TZ-M8 §3.4); имена в SQL не приходят извне
    PLAN_PROFILE_COLUMNS = (
        "mode", "default_days", "weekly_budget_kop", "cuisines", "cuisine_mode",
        "weekday_max_minutes", "weekend_max_minutes", "breakfast_max_minutes",
        "meals", "allow_leftovers", "novelty", "max_repeats_per_horizon",
    )
    PLAN_PROFILE_JSON_COLUMNS = frozenset({"cuisines", "meals"})

    async def plan_profile(self, session: dict[str, Any]) -> dict[str, Any]:
        """Профиль планирования семьи; без записи — значения по умолчанию."""
        row = await self.db().fetchrow(
            f"""
            SELECT {', '.join(self.PLAN_PROFILE_COLUMNS)}
            FROM app_core.household_plan_profiles WHERE household_id=$1
            """,
            session["household_id"],
        )
        profile = dict(DEFAULT_PLAN_PROFILE)
        if row:
            stored = row_dict(row)
            for column in self.PLAN_PROFILE_COLUMNS:
                value = stored.get(column)
                if column in self.PLAN_PROFILE_JSON_COLUMNS:
                    value = _json_column(value)
                if value is not None or column in PLAN_PROFILE_NULLABLE:
                    profile[column] = value
        return profile

    async def save_plan_profile(
        self, session: dict[str, Any], changes: dict[str, Any]
    ) -> dict[str, Any]:
        """Сохраняет профиль целиком (UPSERT): форма присылает все поля."""
        if session["role"] not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для изменения настроек")
        profile = {**DEFAULT_PLAN_PROFILE, **{
            key: value for key, value in changes.items()
            if key in self.PLAN_PROFILE_COLUMNS
        }}
        values = [
            json.dumps(profile[column], ensure_ascii=False)
            if column in self.PLAN_PROFILE_JSON_COLUMNS
            else profile[column]
            for column in self.PLAN_PROFILE_COLUMNS
        ]
        placeholders = ", ".join(
            f"${index + 2}" + ("::jsonb" if column in self.PLAN_PROFILE_JSON_COLUMNS else "")
            for index, column in enumerate(self.PLAN_PROFILE_COLUMNS)
        )
        assignments = ", ".join(
            f"{column}=EXCLUDED.{column}" for column in self.PLAN_PROFILE_COLUMNS
        )
        await self.db().execute(
            f"""
            INSERT INTO app_core.household_plan_profiles (
                household_id, {', '.join(self.PLAN_PROFILE_COLUMNS)}
            ) VALUES ($1, {placeholders})
            ON CONFLICT (household_id) DO UPDATE
            SET {assignments}, updated_at=CURRENT_TIMESTAMP
            """,
            session["household_id"], *values,
        )
        await self.audit(session, "settings.plan_profile_updated", "household", session["household_id"])
        return profile

    async def dashboard(self, session: dict[str, Any]) -> dict[str, Any]:
        household_id = session["household_id"]
        row = await self.db().fetchrow(
            """
            SELECT
                (SELECT count(*) FROM recipe_library.recipes
                 WHERE review_status <> 'rejected') AS recipes,
                (SELECT count(*) FROM recipe_library.recipes
                 WHERE review_status = 'ready') AS recipes_ready,
                (SELECT count(*) FROM recipe_library.sources WHERE import_status='completed') AS sources,
                (SELECT count(*) FROM lenta_store.store_listings WHERE store_code=$2 AND available_for_order) AS products,
                (SELECT count(*) FROM app_core.inventory_lots WHERE household_id=$1) AS inventory,
                (SELECT count(*) FROM app_core.inventory_lots WHERE household_id=$1 AND expires_on <= CURRENT_DATE + 3) AS expiring
            """,
            household_id, STORE_CODE,
        )
        latest_plan = await self.db().fetchrow(
            """
            SELECT id, starts_on, days, estimated_cost_kop, created_at
            FROM app_core.meal_plans WHERE household_id=$1 ORDER BY created_at DESC LIMIT 1
            """,
            household_id,
        )
        result = row_dict(row)
        result["latest_plan"] = row_dict(latest_plan) if latest_plan else None
        return result

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
        """Страница списка рецептов.

        A3: отбор и подсчёт делает SQL. Раньше выбиралось вчетверо больше строк,
        часть отсеивалась в Python и обрезалась по счётчику — совпадения за
        отсечкой терялись, а пользователь видел «не найдено». Имена ингредиентов
        выбирались и тут же выбрасывались, поэтому карточки были пустыми.
        """
        limit = min(max(int(limit), 1), 100)
        offset = max(int(offset), 0)
        rows = await self.db().fetch(
            """
            SELECT r.id, r.title, r.source_page_start, r.source_page_end,
                   r.source_servings_min, r.source_servings_max, r.source_yield_text,
                   r.cuisine_code, r.cuisine_codes, r.dish_type, r.meal_types,
                   r.diet_tags, r.appliances,
                   r.review_status, r.ingredient_count, r.step_count,
                   r.time_total_minutes, r.extraction_confidence,
                   ARRAY(
                       SELECT COALESCE(NULLIF(i.normalized_name, ''), i.ingredient_text)
                       FROM recipe_library.recipe_ingredients i
                       WHERE i.recipe_id=r.id
                       ORDER BY i.position
                       LIMIT 6
                   ) AS ingredient_names,
                   count(*) OVER () AS total_count
            FROM recipe_library.recipes r
            WHERE r.review_status <> 'rejected'
              AND ($1 = '' OR r.title ILIKE '%' || $1 || '%')
              AND ($2 = '' OR jsonb_exists(r.cuisine_codes, $2))
              AND ($3 = '' OR r.meal_types ? $3 OR r.meal_types='[]'::jsonb)
              AND ($4 = FALSE OR r.review_status = 'ready')
              AND ($7 = '' OR r.dish_type=$7)
            -- По названию не сортируем: коллация базы (C.UTF-8) ставит латиницу
            -- строго раньше кириллицы, и первая страница оказывалась целиком
            -- из англоязычных книг. r.id даёт порядок загрузки и стабильный
            -- OFFSET: без него строки с равной уверенностью тасуются между
            -- страницами и часть рецептов не показывается никогда.
            ORDER BY (r.review_status = 'ready') DESC,
                     r.extraction_confidence DESC NULLS LAST,
                     r.id
            LIMIT $5 OFFSET $6
            """,
            search.strip(), cuisine, meal_type, ready_only, limit, offset,
            dish_type,
        )
        total = int(rows[0]["total_count"]) if rows else 0
        items: list[dict[str, Any]] = []
        for row in rows:
            recipe = row_dict(row)
            recipe.pop("total_count", None)
            recipe["ingredient_names"] = [name for name in (recipe.get("ingredient_names") or []) if name]
            recipe["title"] = clean_dish_title(recipe["title"])
            for column in ("meal_types", "diet_tags", "appliances"):
                recipe[column] = _json_column(recipe.get(column))
            items.append(recipe)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    async def recipe_facets(self) -> dict[str, Any]:
        """Какие кухни и приёмы пищи реально встречаются в библиотеке.

        Интерфейс по этому списку решает, показывать ли фильтр: пока кухни не
        размечены, фильтр по кухне отфильтровал бы результат в ноль.
        """
        cuisines = await self.db().fetch(
            """
            SELECT DISTINCT code AS cuisine_code
            FROM recipe_library.recipes r,
                 LATERAL jsonb_array_elements_text(r.cuisine_codes) AS code
            WHERE r.review_status <> 'rejected'
            ORDER BY code
            """
        )
        meal_types = await self.db().fetch(
            """
            SELECT DISTINCT jsonb_array_elements_text(meal_types) AS meal_type
            FROM recipe_library.recipes
            WHERE review_status <> 'rejected'
            ORDER BY meal_type
            """
        )
        dish_types = await self.db().fetch(
            """
            SELECT dish_type, count(*) AS cnt FROM recipe_library.recipes
            WHERE review_status <> 'rejected' AND dish_type IS NOT NULL
            GROUP BY dish_type ORDER BY cnt DESC
            """
        )
        return {
            "cuisines": [row["cuisine_code"] for row in cuisines],
            "meal_types": [row["meal_type"] for row in meal_types],
            "dish_types": [row["dish_type"] for row in dish_types],
        }

    async def set_review_status(
        self, session: dict[str, Any], recipe_id: int, status: str
    ) -> dict[str, Any] | None:
        """Смена статуса проверки рецепта (A4).

        Библиотека рецептов общая для всех семей — отдельной таблицы
        ``recipe_reviews`` в схеме пока нет (TZ-M2 §3), поэтому правка глобальная
        и её след остаётся в ``audit_log``.
        """
        if session["role"] not in {"owner", "admin"}:
            raise PermissionError("Недостаточно прав для проверки рецептов")
        if status not in REVIEW_STATUSES:
            raise ValueError("Неизвестный статус проверки")
        row = await self.db().fetchrow(
            """
            UPDATE recipe_library.recipes
            SET review_status=$2, updated_at=CURRENT_TIMESTAMP
            WHERE id=$1
            RETURNING id, title, review_status
            """,
            recipe_id, status,
        )
        if not row:
            return None
        await self.audit(session, "recipe.reviewed", "recipe", recipe_id, {"review_status": status})
        result = row_dict(row)
        result["title"] = clean_dish_title(result["title"])
        return result

    async def recipe_detail(
        self, recipe_id: int, household_id: Any = None
    ) -> dict[str, Any] | None:
        recipe = await self.db().fetchrow(
            """
            SELECT r.id, r.title, r.source_page_start, r.source_page_end,
                   r.source_servings_min, r.source_servings_max, r.source_yield_text,
                   r.cuisine_code, r.cuisine_codes, r.meal_types, r.diet_tags, r.appliances,
                   r.review_status, r.review_reasons, r.ingredient_count, r.step_count,
                   r.time_total_minutes, r.extraction_confidence
            FROM recipe_library.recipes r
            WHERE r.id=$1 AND r.review_status <> 'rejected'
            """,
            recipe_id,
        )
        if not recipe:
            return None
        # S5: source_id и fingerprint не выбираются вовсе — раньше здесь стоял
        # `SELECT r.*`, и наружу утекали ссылки на книгу-источник.
        # Фильтры качества по названию сняты намеренно: рецепт, видимый в списке,
        # обязан открываться по прямой ссылке (A3, приёмка Б2).
        ingredients = await self.db().fetch(
            """
            SELECT position, raw_text, quantity_min, quantity_max, unit_raw, unit_code,
                   ingredient_text, normalized_name, parsing_confidence,
                   is_to_taste, section, note
            FROM recipe_library.recipe_ingredients WHERE recipe_id=$1 ORDER BY position
            """,
            recipe_id,
        )
        steps = await self.db().fetch(
            "SELECT position, instruction FROM recipe_library.recipe_steps WHERE recipe_id=$1 ORDER BY position",
            recipe_id,
        )
        product_matcher = await self.product_matcher()
        enriched_ingredients: list[dict[str, Any]] = []
        for ingredient_row in ingredients:
            ingredient = row_dict(ingredient_row)
            source_quantity = ingredient.get("quantity_max") or ingredient.get("quantity_min")
            base_quantity, base_unit = _base_quantity(
                Decimal(str(source_quantity)) if source_quantity is not None else None,
                ingredient.get("unit_code"),
            )
            product = product_matcher.match(
                str(ingredient.get("normalized_name") or ingredient.get("ingredient_text") or ""),
                base_unit,
                "balanced",
                base_quantity,
            )
            ingredient["matched_product"] = (
                {
                    "id": product["id"],
                    "name": product["name"],
                    "url": product.get("url"),
                    "pack_text": product.get("pack_text"),
                    "pack_quantity": product.get("pack_quantity"),
                    "pack_unit": product.get("pack_unit"),
                    "regular_price_kop": product.get("regular_price_kop"),
                    "loyalty_price_kop": product.get("loyalty_price_kop"),
                    "promo_price_kop": product.get("promo_price_kop"),
                    "effective_price_kop": product.get("effective_price_kop"),
                }
                if product else None
            )
            enriched_ingredients.append(ingredient)

        result = row_dict(recipe)
        result["title"] = clean_dish_title(result["title"])
        for column in ("meal_types", "diet_tags", "appliances", "review_reasons"):
            result[column] = _json_column(result.get(column))
        result["ingredients"] = enriched_ingredients
        result["steps"] = [row_dict(row) for row in steps]
        if household_id is not None:
            result["my_rating"] = await self.db().fetchval(
                "SELECT rating FROM app_core.recipe_ratings WHERE household_id=$1 AND recipe_id=$2",
                household_id, recipe_id,
            )
        return result

    async def set_recipe_rating(
        self, session: dict[str, Any], recipe_id: int, rating: int | None
    ) -> dict[str, Any] | None:
        """Оценка рецепта семьёй: 1–5 звёзд, null — снять оценку."""
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет ставить оценки")
        exists = await self.db().fetchval(
            "SELECT 1 FROM recipe_library.recipes WHERE id=$1 AND review_status <> 'rejected'",
            recipe_id,
        )
        if not exists:
            return None
        if rating is None:
            await self.db().execute(
                "DELETE FROM app_core.recipe_ratings WHERE household_id=$1 AND recipe_id=$2",
                session["household_id"], recipe_id,
            )
        else:
            if not 1 <= int(rating) <= 5:
                raise ValueError("Оценка — от 1 до 5")
            await self.db().execute(
                """
                INSERT INTO app_core.recipe_ratings (household_id, recipe_id, rating, updated_by)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (household_id, recipe_id)
                DO UPDATE SET rating=EXCLUDED.rating, updated_by=EXCLUDED.updated_by,
                              updated_at=CURRENT_TIMESTAMP
                """,
                session["household_id"], recipe_id, int(rating), session["user_id"],
            )
        if rating is None:
            # Снятие оценки убирает и её след: «передумали» — не мнение.
            await self.db().execute(
                """
                DELETE FROM app_core.taste_events
                WHERE household_id=$1 AND recipe_id=$2 AND kind='rated'
                """,
                session["household_id"], recipe_id,
            )
        else:
            await self.record_taste_event(
                session, recipe_id, "rated", rating=int(rating)
            )
        await self.audit(session, "recipe.rated", "recipe", recipe_id, {"rating": rating})
        return {"recipe_id": recipe_id, "my_rating": rating}

    async def record_taste_event(
        self,
        session: dict[str, Any],
        recipe_id: int,
        kind: str,
        *,
        rating: int | None = None,
        person_id: uuid.UUID | None = None,
        channel: str = "web",
        connection: Any = None,
    ) -> None:
        """Факт из жизни семьи, который учит планировщик (TZ-M8 §4.1)."""
        executor = connection or self.db()
        await executor.execute(
            """
            INSERT INTO app_core.taste_events (
                household_id, person_id, recipe_id, kind, value, channel
            ) VALUES ($1,$2,$3,$4,$5,$6)
            """,
            session["household_id"], person_id, int(recipe_id), kind,
            event_value(kind, rating), channel,
        )

    async def list_products(
        self,
        search: str = "",
        sort: str = "name",
        limit: int = 100,
        offset: int = 0,
        discount_only: bool = False,
        category: str = "",
    ) -> dict[str, Any]:
        order_sql = {
            "price_asc": "effective_price_kop ASC NULLS LAST, p.name, p.id",
            "price_desc": "effective_price_kop DESC NULLS LAST, p.name, p.id",
        }.get(sort, "p.name, p.id")
        limit = min(max(int(limit), 1), 300)
        offset = max(int(offset), 0)
        rows = await self.db().fetch(
            f"""
            SELECT p.id, p.name, p.brand, p.pack_text, p.pack_quantity, p.pack_unit,
                   p.kcal_100, p.protein_100, p.fat_100, p.carb_100, p.url,
                   ph.regular_price_kop, ph.loyalty_price_kop, ph.promo_price_kop,
                   ph.discount_percent,
                   COALESCE(NULLIF(ph.promo_price_kop,0), NULLIF(ph.loyalty_price_kop,0), ph.regular_price_kop) AS effective_price_kop,
                   ph.observed_on, l.available_for_order,
                   count(*) OVER () AS total_count
            FROM lenta_store.store_products p
            JOIN lenta_store.store_listings l ON l.product_id=p.id AND l.store_code=$3
            LEFT JOIN LATERAL (
                SELECT * FROM lenta_store.store_price_history h
                WHERE h.product_id=p.id AND h.store_code=l.store_code
                ORDER BY h.observed_at DESC LIMIT 1
            ) ph ON TRUE
            WHERE l.available_for_order
              AND ($1='' OR p.name ILIKE '%' || $1 || '%' OR COALESCE(p.brand,'') ILIKE '%' || $1 || '%')
              -- «со скидкой» — именно акционная цена. discount_percent сюда не
              -- годится: он считается от цены по Карте №1 и стоит почти у всех.
              AND ($2 = FALSE OR (
                    NULLIF(ph.promo_price_kop, 0) IS NOT NULL
                    AND ph.promo_price_kop < ph.regular_price_kop
              ))
              AND ($6 = '' OR EXISTS (
                    SELECT 1 FROM lenta_store.store_product_categories c
                    WHERE c.product_id=p.id AND c.category_slug=$6
              ))
            ORDER BY {order_sql}
            LIMIT $4 OFFSET $5
            """,
            search.strip(), discount_only, STORE_CODE, limit, offset, category.strip(),
        )
        total = int(rows[0]["total_count"]) if rows else 0
        items = []
        for row in rows:
            product = row_dict(row)
            product.pop("total_count", None)
            items.append(product)
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    async def product_categories(self) -> list[dict[str, Any]]:
        """Разделы каталога с числом доступных товаров — для фильтра на витрине."""
        rows = await self.db().fetch(
            """
            SELECT c.category_slug, count(DISTINCT c.product_id) AS product_count
            FROM lenta_store.store_product_categories c
            JOIN lenta_store.store_listings l
              ON l.product_id=c.product_id AND l.store_code=$1 AND l.available_for_order
            GROUP BY c.category_slug
            ORDER BY product_count DESC, c.category_slug
            """,
            STORE_CODE,
        )
        return [row_dict(row) for row in rows]

    async def product_match_candidates(self) -> list[dict[str, Any]]:
        rows = await self.db().fetch(
            """
            SELECT p.id, p.name, p.brand, p.pack_text, p.pack_quantity, p.pack_unit,
                   p.kcal_100, p.protein_100, p.fat_100, p.carb_100, p.url,
                   ph.regular_price_kop, ph.loyalty_price_kop, ph.promo_price_kop,
                   COALESCE(NULLIF(ph.promo_price_kop,0), NULLIF(ph.loyalty_price_kop,0), ph.regular_price_kop) AS effective_price_kop,
                   ARRAY(
                       SELECT c.category_slug
                       FROM lenta_store.store_product_categories c
                       WHERE c.product_id=p.id
                       ORDER BY c.category_slug
                   ) AS category_slugs
            FROM lenta_store.store_products p
            JOIN lenta_store.store_listings l
              ON l.product_id=p.id
             AND l.store_code='lenta-155'
             AND l.available_for_order
            JOIN LATERAL (
                SELECT * FROM lenta_store.store_price_history h
                WHERE h.product_id=p.id AND h.store_code=l.store_code
                ORDER BY h.observed_at DESC LIMIT 1
            ) ph ON TRUE
            WHERE COALESCE(NULLIF(ph.promo_price_kop,0), NULLIF(ph.loyalty_price_kop,0), ph.regular_price_kop) IS NOT NULL
            """
        )
        return [row_dict(row) for row in rows]

    async def catalogue_stamp(self) -> Any:
        """Отметка ревизии каталога — по ней инвалидируется кэш матчера."""
        return await self.db().fetchval(
            "SELECT max(observed_on) FROM lenta_store.store_price_history WHERE store_code=$1",
            STORE_CODE,
        )

    async def product_matcher(self) -> ProductMatcher:
        return await self.product_cache.get(self.catalogue_stamp, self.product_match_candidates)

    async def list_inventory(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        rows = await self.db().fetch(
            """
            SELECT id, name, quantity, unit_code, expires_on, storage_area, created_at
            FROM app_core.inventory_lots WHERE household_id=$1
            ORDER BY expires_on NULLS LAST, created_at DESC
            """,
            session["household_id"],
        )
        return [row_dict(row) for row in rows]

    async def add_inventory(self, session: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять запасы")
        item_id = uuid.uuid4()
        row = await self.db().fetchrow(
            """
            INSERT INTO app_core.inventory_lots (
                id, household_id, name, quantity, unit_code, expires_on,
                storage_area, created_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id, name, quantity, unit_code, expires_on, storage_area, created_at
            """,
            item_id,
            session["household_id"],
            str(item["name"]).strip()[:120],
            Decimal(str(item["quantity"])),
            item["unit_code"],
            item.get("expires_on"),
            item.get("storage_area", "fridge"),
            session["user_id"],
        )
        await self.audit(session, "inventory.added", "inventory_lot", item_id, {"name": item["name"]})
        return row_dict(row)

    async def delete_inventory(self, session: dict[str, Any], item_id: uuid.UUID) -> bool:
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять запасы")
        result = await self.db().execute(
            "DELETE FROM app_core.inventory_lots WHERE id=$1 AND household_id=$2",
            item_id,
            session["household_id"],
        )
        deleted = affected_rows(result) == 1
        if deleted:
            await self.audit(session, "inventory.deleted", "inventory_lot", item_id)
        return deleted

    async def planner_recipe_pool(
        self, cuisines: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Пул рецептов планировщика: общее окно плюс окно выбранных кухонь.

        A5/B5: раньше сюда грузилась вся библиотека вместе с ингредиентами как
        JSONB, без LIMIT. Берём ready в первую очередь; черновики
        (needs_review) идут следом — build_plan доберёт их, только если готовых
        рецептов меньше порога (TZ-M5R §2.1). Запрос вынесен из planner_data,
        потому что тем же пулом греется кэш (N1).

        Общее окно ранжируется по уверенности распознавания и потому почти не
        содержит редких кухонь — выбранные добираются отдельным окном, иначе
        фильтр кухни в build_plan остаётся без кандидатов.
        """
        recipe_rows = await self.db().fetch(
            """
            WITH picked AS (
                SELECT r.id, r.title, r.source_page_start, r.source_servings_min,
                       r.cuisine_code, r.cuisine_codes, r.meal_types, r.appliances,
                       r.review_status, r.extraction_confidence, r.dish_type,
                       r.diet_tags, r.time_total_minutes
                FROM recipe_library.recipes r
                WHERE r.review_status IN ('ready', 'needs_review')
                  AND r.ingredient_count >= 3
                  AND r.step_count >= 1
                ORDER BY (r.review_status = 'ready') DESC,
                         r.extraction_confidence DESC NULLS LAST, r.id
                LIMIT $1
            )
            , cuisine_picked AS (
                SELECT r.id, r.title, r.source_page_start, r.source_servings_min,
                       r.cuisine_code, r.cuisine_codes, r.meal_types, r.appliances,
                       r.review_status, r.extraction_confidence, r.dish_type,
                       r.diet_tags, r.time_total_minutes
                FROM recipe_library.recipes r
                WHERE r.review_status IN ('ready', 'needs_review')
                  AND r.ingredient_count >= 3
                  AND r.step_count >= 1
                  AND jsonb_exists_any(r.cuisine_codes, $2::text[])
                  AND r.id NOT IN (SELECT id FROM picked)
                ORDER BY (r.review_status = 'ready') DESC,
                         r.extraction_confidence DESC NULLS LAST, r.id
                LIMIT $3
            ), pool AS (
                SELECT * FROM picked
                UNION ALL
                SELECT * FROM cuisine_picked
            )
            SELECT p.*, COALESCE(ing.items, '[]'::jsonb) AS ingredients
            FROM pool p
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'ingredient_text', i.ingredient_text,
                    -- Унифицированное имя из справочника (волна Haiku):
                    -- «масло сливочное», «butter» и «сливочного масла» —
                    -- одна позиция. Догадки ниже 0.7 не подставляются,
                    -- исходное имя остаётся запасным вариантом.
                    'normalized_name', COALESCE(
                        NULLIF(c.canonical_name, ''), i.normalized_name
                    ),
                    'source_name', i.normalized_name,
                    'base_name', c.base_name,
                    'quantity_min', i.quantity_min,
                    'quantity_max', i.quantity_max,
                    'unit_code', i.unit_code,
                    'is_to_taste', i.is_to_taste
                ) ORDER BY i.position) AS items
                FROM recipe_library.recipe_ingredients i
                LEFT JOIN recipe_library.ingredient_canonical c
                       ON c.raw_name = i.normalized_name
                      AND c.is_ingredient
                      AND COALESCE(c.confidence, 0) >= 0.7
                WHERE i.recipe_id = p.id
                  -- Мусор из книг («правильно резать лук 122») в покупки
                  -- не попадает.
                  AND NOT EXISTS (
                        SELECT 1 FROM recipe_library.ingredient_canonical x
                        WHERE x.raw_name = i.normalized_name
                          AND NOT x.is_ingredient
                  )
            ) ing ON TRUE
            """,
            PLANNER_RECIPE_LIMIT,
            [str(code) for code in (cuisines or [])],
            PLANNER_CUISINE_LIMIT,
        )
        recipes = [dict(row) for row in recipe_rows]
        for recipe in recipes:
            if isinstance(recipe["ingredients"], str):
                recipe["ingredients"] = json.loads(recipe["ingredients"])
        return recipes

    async def warm_planner_caches(self) -> int:
        """Прогревает то, за что раньше платил первый клик «Составить меню» (N1).

        Тяжёлого здесь два: запрос пула рецептов (страницы Postgres остывают за
        часы простоя) и мемоизация матчера — она живёт внутри экземпляра, а он
        пересоздаётся при каждом обновлении каталога. Сопоставления считаются в
        отдельном потоке: это чистый Python на несколько секунд.
        """
        recipes = await self.planner_recipe_pool()
        matcher = await self.product_matcher()
        if matcher.warmed:
            return 0
        warmed = await asyncio.to_thread(warm_product_matcher, matcher, recipes)
        matcher.warmed = True
        return warmed

    async def plan_history(
        self, session: dict[str, Any], starts_on: date
    ) -> list[dict[str, Any]]:
        """Блюда семьи за окно ротации — из всех планов, не только последнего.

        Без этого «новое меню» повторяло вчерашний ужин: планировщик просто не
        знал, что семья уже ела (дефект P1).
        """
        rows = await self.db().fetch(
            """
            SELECT pm.recipe_id, pm.meal_date, r.dish_type
            FROM app_core.plan_meals pm
            JOIN app_core.meal_plans mp ON mp.id = pm.plan_id
            JOIN recipe_library.recipes r ON r.id = pm.recipe_id
            WHERE mp.household_id = $1
              AND pm.meal_date >= $2::date - $3::int
              AND pm.meal_date < $2::date
            """,
            session["household_id"], starts_on, HISTORY_WINDOW_DAYS,
        )
        return [row_dict(row) for row in rows]

    async def planner_data(
        self,
        session: dict[str, Any],
        cuisines: list[str] | None = None,
        starts_on: date | None = None,
    ) -> dict[str, Any]:
        household_id = session["household_id"]
        people = [row_dict(row) for row in await self.db().fetch(
            """
            SELECT id, name, person_type, target_kcal, portion_factor,
                   birth_date, sex, height_cm, weight_kg, activity, goal,
                   protein_share, fat_share, carb_share, meal_shares, eats_meals
            FROM app_core.people WHERE household_id=$1 ORDER BY position
            """,
            household_id,
        )]
        appliances = [row["appliance_code"] for row in await self.db().fetch(
            "SELECT appliance_code FROM app_core.appliances WHERE household_id=$1", household_id
        )]
        rules = [row_dict(row) for row in await self.db().fetch(
            """
            SELECT rule_type, term, is_hard, person_id, diet_tag
            FROM app_core.dietary_rules WHERE household_id=$1
            """,
            household_id,
        )]
        inventory = [dict(row) for row in await self.db().fetch(
            "SELECT name, quantity, unit_code, expires_on FROM app_core.inventory_lots WHERE household_id=$1 AND quantity>0",
            household_id,
        )]
        recipes = await self.planner_recipe_pool(cuisines)
        nutrition_rows = await self.db().fetch(
            """
            SELECT name, kcal_100, protein_100, fat_100, carb_100, piece_mass_g
            FROM recipe_library.ingredient_nutrition
            """
        )
        nutrition = {row["name"]: dict(row) for row in nutrition_rows}
        matcher = await self.product_matcher()
        return {
            "people": people,
            "appliances": appliances,
            "rules": rules,
            "inventory": inventory,
            "recipes": recipes,
            "products": matcher.products,
            "product_matcher": matcher,
            "nutrition": nutrition,
            "synonyms": await self.ingredient_synonyms(),
            "ratings": {
                int(row["recipe_id"]): int(row["rating"])
                for row in await self.db().fetch(
                    "SELECT recipe_id, rating FROM app_core.recipe_ratings WHERE household_id=$1",
                    household_id,
                )
            },
            "history": await self.plan_history(session, starts_on or date.today()),
            "plan_profile": await self.plan_profile(session),
            "taste_events": await self.taste_events(session),
        }

    async def ingredient_synonyms(self) -> list[dict[str, Any]]:
        rows = await self.db().fetch(
            "SELECT term, canonical, kind FROM app_core.ingredient_synonyms"
        )
        return [row_dict(row) for row in rows]

    async def add_to_plan(
        self,
        session: dict[str, Any],
        plan_id: uuid.UUID,
        meal_date: date,
        meal_type: str,
        recipe_id: int,
    ) -> dict[str, Any] | None:
        """Поставить конкретный рецепт в слот плана (TZ-M7 §5.7).

        Обёртка над ``replace_meal``: та же проверка допустимости для слота и
        та же пересборка списка покупок — разница только в том, что слот
        задан датой и приёмом пищи, а не идентификатором блюда.
        """
        meal_id = await self.db().fetchval(
            """
            SELECT pm.id
            FROM app_core.plan_meals pm
            JOIN app_core.meal_plans p ON p.id = pm.plan_id AND p.household_id = $1
            WHERE pm.plan_id = $2 AND pm.meal_date = $3 AND pm.meal_type = $4
            """,
            session["household_id"], plan_id, meal_date, meal_type,
        )
        if meal_id is None:
            return None
        return await self.replace_meal(session, plan_id, meal_id, int(recipe_id))

    async def create_plan(
        self,
        session: dict[str, Any],
        *,
        starts_on: date,
        days: int | None = None,
        budget_kop: int | None = None,
        cuisines: list[str] | None = None,
        price_tier: str | None = None,
        mode: str | None = None,
        meals: list[str] | None = None,
        allow_leftovers: bool | None = None,
    ) -> dict[str, Any]:
        """Собрать и сохранить план: сырьё → решатель → БД → готовый payload.

        Живёт в слое данных, а не в HTTP-обработчике, потому что то же самое
        делает бот (TZ-M7 §2: веб и бот вызывают один слой). ``ValueError`` —
        нечего ставить в слоты, ``PermissionError`` — роль только смотрит.

        Незаполненные поля берутся из профиля семьи (TZ-M8 §3.4) — и в вебе, и
        в чате. Переданное явно действует только на этот план и обратно в
        профиль не пишется: «на этот раз без обедов» не должно менять
        настройки семьи.
        """
        if session.get("role") == "viewer":
            raise PermissionError("Режим просмотра не позволяет создавать планы")
        # Явно переданную стратегию проверяем до любых запросов: отказ по
        # заведомо неверному вводу не должен стоить чтения профиля.
        if price_tier is not None and price_tier not in PRICE_TIERS:
            raise ValueError(UNKNOWN_TIER_TEXT)
        profile = await self.plan_profile(session)
        days = days or int(profile["default_days"])
        cuisines = list(cuisines if cuisines is not None else profile["cuisines"])
        meals = list(meals if meals is not None else profile["meals"])
        mode = mode or str(profile["mode"])
        price_tier = price_tier or price_tier_for(mode)
        if price_tier not in PRICE_TIERS:
            raise ValueError(UNKNOWN_TIER_TEXT)
        if budget_kop is None and profile.get("weekly_budget_kop"):
            # Недельный бюджет семьи растягивается на горизонт плана.
            budget_kop = int(int(profile["weekly_budget_kop"]) * days / 7)
        if allow_leftovers is not None:
            profile = {**profile, "allow_leftovers": allow_leftovers}
        data = await self.planner_data(session, cuisines, starts_on)
        data["plan_profile"] = profile
        # K7: скоринг 500 рецептов и CP-SAT занимают до десятков секунд —
        # в отдельном потоке, иначе замирает весь event loop (и /health).
        plan = await asyncio.to_thread(
            functools.partial(
                build_plan,
                household_id=str(session["household_id"]),
                starts_on=starts_on,
                days=days,
                cuisines=cuisines,
                price_tier=price_tier,
                mode=mode,
                budget_kop=budget_kop,
                meals=meals,
                cuisine_mode=str(profile["cuisine_mode"]),
                **data,
            )
        )
        plan_id = await self.save_plan(
            session, starts_on, days, budget_kop, cuisines, price_tier, plan, mode
        )
        plan["id"] = plan_id
        plan["starts_on"] = starts_on
        plan["days"] = days
        plan["budget_kop"] = budget_kop
        plan["mode"] = mode
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
        mode: str = "balanced",
    ) -> str:
        plan_id = uuid.uuid4()
        async with self.db().acquire() as connection, connection.transaction():
            await connection.execute(
                """
                INSERT INTO app_core.meal_plans (
                    id, household_id, starts_on, days, budget_kop, estimated_cost_kop,
                    matched_cost_items, total_cost_items, cuisine_preferences,
                    price_tier, created_by, solver_status, plan_warnings, mode
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13::jsonb,$14)
                """,
                plan_id, session["household_id"], starts_on, days, budget_kop,
                plan["estimated_cost_kop"], plan["matched_cost_items"], plan["total_cost_items"],
                json.dumps(cuisines, ensure_ascii=False), price_tier, session["user_id"],
                plan.get("solver_status"),
                json.dumps(plan.get("warnings") or [], ensure_ascii=False),
                mode,
            )
            # Идентификаторы раздаются заранее: блюдо-наследник ссылается на
            # ужин-источник, а планировщик знает только его позицию в плане.
            meal_ids = [uuid.uuid4() for _ in plan["meals"]]
            for position, meal in enumerate(plan["meals"], 1):
                source = meal.get("leftover_of")
                await connection.execute(
                    """
                    INSERT INTO app_core.plan_meals (
                        id, plan_id, meal_date, meal_type, recipe_id, scale,
                        servings, estimated_kcal, position, warnings,
                        estimated_protein, estimated_fat, estimated_carb, reasons,
                        leftover_of
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13,$14::jsonb,$15)
                    """,
                    meal_ids[position - 1], plan_id, meal["meal_date"], meal["meal_type"],
                    meal["recipe_id"], meal["scale"], meal["servings"],
                    meal["estimated_kcal"], position,
                    json.dumps(meal.get("warnings") or [], ensure_ascii=False),
                    meal.get("estimated_protein"), meal.get("estimated_fat"),
                    meal.get("estimated_carb"),
                    json.dumps(meal.get("reasons") or [], ensure_ascii=False),
                    meal_ids[source - 1] if source else None,
                )
            for item in plan["shopping"]:
                await connection.execute(
                    """
                    INSERT INTO app_core.plan_ingredients (
                        id, plan_id, normalized_name, quantity, unit_code,
                        covered_from_inventory, buy_quantity, matched_product_id,
                        pack_count, estimated_cost_kop, to_taste
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    """,
                    uuid.uuid4(), plan_id, item["normalized_name"], item["quantity"],
                    item["unit_code"], item["covered_from_inventory"], item["buy_quantity"],
                    item["matched_product_id"], item["pack_count"], item["estimated_cost_kop"],
                    bool(item.get("to_taste")),
                )
            # «Показано» — не мнение, но без него не отличить «не пробовали»
            # от «пробовали и промолчали» (§4.1).
            for meal in plan["meals"]:
                await self.record_taste_event(
                    session, int(meal["recipe_id"]), "planned", connection=connection
                )
            await self._audit(connection, session["household_id"], session["user_id"], "meal_plan.generated", "meal_plan", plan_id, {"days": days})
        return str(plan_id)

    PLAN_HEADER_COLUMNS = """
        id, starts_on, days, budget_kop, estimated_cost_kop,
        matched_cost_items, total_cost_items, cuisine_preferences,
        price_tier, status, created_at, solver_status, plan_warnings
    """

    async def latest_plan(self, session: dict[str, Any]) -> dict[str, Any] | None:
        header = await self.db().fetchrow(
            f"""
            SELECT {self.PLAN_HEADER_COLUMNS}
            FROM app_core.meal_plans WHERE household_id=$1 ORDER BY created_at DESC LIMIT 1
            """,
            session["household_id"],
        )
        return await self._plan_payload(header) if header else None

    async def get_plan(
        self, session: dict[str, Any], plan_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """План по идентификатору. Чужой household даёт None → маршрут вернёт 404,
        а не 403: факт существования плана наружу не утекает."""
        header = await self.db().fetchrow(
            f"""
            SELECT {self.PLAN_HEADER_COLUMNS}
            FROM app_core.meal_plans WHERE id=$1 AND household_id=$2
            """,
            plan_id, session["household_id"],
        )
        return await self._plan_payload(header) if header else None

    async def list_plans(self, session: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.db().fetch(
            f"""
            SELECT {self.PLAN_HEADER_COLUMNS},
                   (SELECT count(*) FROM app_core.plan_meals m WHERE m.plan_id = p.id) AS meal_count
            FROM app_core.meal_plans p
            WHERE p.household_id=$1
            ORDER BY p.created_at DESC
            LIMIT $2
            """,
            session["household_id"], min(max(int(limit), 1), 100),
        )
        result = []
        for row in rows:
            plan = row_dict(row)
            plan["cuisine_preferences"] = _json_column(plan.get("cuisine_preferences"))
            plan.pop("plan_warnings", None)  # в списке планов предупреждения не нужны
            result.append(plan)
        return result

    async def delete_plan(self, session: dict[str, Any], plan_id: uuid.UUID) -> bool:
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет удалять планы")
        result = await self.db().execute(
            "DELETE FROM app_core.meal_plans WHERE id=$1 AND household_id=$2",
            plan_id, session["household_id"],
        )
        deleted = affected_rows(result) == 1
        if deleted:
            await self.audit(session, "meal_plan.deleted", "meal_plan", plan_id)
        return deleted

    async def mark_purchased(
        self,
        session: dict[str, Any],
        plan_id: uuid.UUID,
        item_id: uuid.UUID,
        purchased: bool,
    ) -> dict[str, Any] | None:
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять список покупок")
        row = await self.db().fetchrow(
            """
            UPDATE app_core.plan_ingredients pi
            SET purchased_at = CASE WHEN $4 THEN CURRENT_TIMESTAMP ELSE NULL END
            FROM app_core.meal_plans mp
            WHERE pi.id=$1 AND pi.plan_id=$2
              AND mp.id = pi.plan_id AND mp.household_id=$3
            RETURNING pi.id, pi.normalized_name, pi.purchased_at
            """,
            item_id, plan_id, session["household_id"], purchased,
        )
        return row_dict(row) if row else None

    async def replace_meal(
        self,
        session: dict[str, Any],
        plan_id: uuid.UUID,
        meal_id: uuid.UUID,
        new_recipe_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Замена блюда (TZ-M5R §3): без recipe_id — топ-10 альтернатив для
        слота при зафиксированных остальных; с recipe_id — применение замены
        с пересборкой списка покупок."""
        from .planner import meal_entry_for, slot_alternatives
        from .planning.scaling import desired_servings as desired_servings_of

        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять план")
        header = await self.db().fetchrow(
            "SELECT id, price_tier, mode, cuisine_preferences FROM app_core.meal_plans "
            "WHERE id=$1 AND household_id=$2",
            plan_id, session["household_id"],
        )
        if not header:
            return None
        meal_rows = await self.db().fetch(
            "SELECT id, meal_date, meal_type, recipe_id, servings FROM app_core.plan_meals WHERE plan_id=$1",
            plan_id,
        )
        target = next((row for row in meal_rows if row["id"] == meal_id), None)
        if target is None:
            return None
        cuisines = [str(item) for item in _json_column(header["cuisine_preferences"])]
        data = await self.planner_data(session, cuisines, target["meal_date"])
        # K7: полный скоринг пула — тяжёлая синхронная работа, в поток.
        alternatives = await asyncio.to_thread(
            functools.partial(
                slot_alternatives,
                meal_date=target["meal_date"],
                meal_type=target["meal_type"],
                current_recipe_id=int(target["recipe_id"]),
                other_meals=[
                    {"recipe_id": row["recipe_id"], "meal_date": row["meal_date"]}
                    for row in meal_rows
                    if row["id"] != meal_id
                ],
                cuisines=cuisines,
                price_tier=header["price_tier"],
                mode=header["mode"],
                limit=MEAL_ALTERNATIVES_LIMIT if new_recipe_id is None else 1000,
                # выбранное вручную блюдо проверяем по жёстким ограничениям,
                # а не по ранжированию слота (TZ-M7 §5.7)
                keep_ids=() if new_recipe_id is None else (int(new_recipe_id),),
                with_details=True,
                **{
                    key: data[key]
                    for key in (
                        "people", "appliances", "rules", "inventory", "recipes",
                        "products", "product_matcher", "synonyms", "ratings",
                        "nutrition", "history", "plan_profile",
                    )
                    if key in data
                },
            )
        )
        if new_recipe_id is None:
            # TZ-M8 §6.6: у каждой карточки своя группа, причина и дельты —
            # «дешевле на 80 ₽» полезнее, чем просто список названий.
            return {
                "alternatives": [
                    {
                        "recipe_id": int(card["recipe"]["id"]),
                        "title": clean_dish_title(card["recipe"]["title"]),
                        "source_page_start": card["recipe"].get("source_page_start"),
                        "review_status": card["recipe"].get("review_status"),
                        "draft": card["recipe"].get("review_status") != "ready",
                        "group": card["group"],
                        "reason": card["reason"],
                        "delta_kcal": card["delta_kcal"],
                        "delta_cost_kop": card["delta_cost_kop"],
                    }
                    for card in alternatives[:MEAL_ALTERNATIVES_LIMIT]
                ]
            }

        chosen = next(
            (
                card
                for card in alternatives
                if int(card["recipe"]["id"]) == int(new_recipe_id)
            ),
            None,
        )
        if chosen is None:
            raise ValueError("Этот рецепт нельзя поставить в выбранный слот")
        selected = chosen["recipe"]
        entry = meal_entry_for(
            selected,
            target["meal_date"],
            target["meal_type"],
            desired_servings_of(data["people"]),
            product_matcher=data["product_matcher"],
            price_tier=str(header["price_tier"] or "balanced"),
            synonyms=data.get("synonyms"),
            nutrition_table=data.get("nutrition"),
        )
        # Причина замены сохраняется вместе с блюдом: план не должен терять
        # объяснение после ручной правки (TZ-M8 §5).
        entry["reasons"] = [chosen["reason"]]
        async with self.db().acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE app_core.plan_meals
                SET recipe_id=$3, scale=$4, servings=$5, estimated_kcal=$6,
                    warnings=$7::jsonb, estimated_protein=$8, estimated_fat=$9,
                    estimated_carb=$10, reasons=$11::jsonb
                WHERE id=$1 AND plan_id=$2
                """,
                meal_id, plan_id, entry["recipe_id"], entry["scale"],
                entry["servings"], entry["estimated_kcal"],
                json.dumps(entry["warnings"], ensure_ascii=False),
                entry.get("estimated_protein"), entry.get("estimated_fat"),
                entry.get("estimated_carb"),
                json.dumps(entry.get("reasons") or [], ensure_ascii=False),
            )
            # Замена — сильный сигнал вкуса: одно блюдо ушло, другое пришло
            # (§4.1). Раньше это оставалось только в audit_log.
            await self.record_taste_event(
                session, int(target["recipe_id"]), "replaced_out", connection=connection
            )
            await self.record_taste_event(
                session, int(entry["recipe_id"]), "replaced_in", connection=connection
            )
            await self._audit(
                connection, session["household_id"], session["user_id"],
                "meal_plan.meal_replaced", "meal_plan", plan_id,
                {"meal_id": str(meal_id), "recipe_id": entry["recipe_id"]},
            )
        await self._rebuild_plan_shopping(session, plan_id)
        return await self.get_plan(session, plan_id)

    async def set_meal_status(
        self, session: dict[str, Any], plan_id: uuid.UUID, meal_id: uuid.UUID, status: str
    ) -> dict[str, Any] | None:
        """«Приготовили» или «пропустили» — самый честный сигнал вкуса (§4.1)."""
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять план")
        if status not in {"cooked", "skipped"}:
            raise ValueError("Статус блюда — cooked или skipped")
        row = await self.db().fetchrow(
            """
            UPDATE app_core.plan_meals pm
            SET status = $4
            FROM app_core.meal_plans mp
            WHERE pm.id=$1 AND pm.plan_id=$2
              AND mp.id = pm.plan_id AND mp.household_id=$3
            RETURNING pm.id, pm.recipe_id, pm.status
            """,
            meal_id, plan_id, session["household_id"], status,
        )
        if not row:
            return None
        await self.record_taste_event(session, int(row["recipe_id"]), status)
        return row_dict(row)

    async def taste_events(
        self, session: dict[str, Any], limit: int = TASTE_EVENTS_LIMIT
    ) -> list[dict[str, Any]]:
        """События вкуса семьи, свежие первыми."""
        rows = await self.db().fetch(
            """
            SELECT recipe_id, person_id, kind, value, created_at
            FROM app_core.taste_events
            WHERE household_id=$1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            session["household_id"], limit,
        )
        return [row_dict(row) for row in rows]

    async def _taste_model(self, session: dict[str, Any]) -> tuple[Any, dict[int, Any]]:
        """Модель вкуса и метаданные рецептов, о которых есть события."""
        events = await self.taste_events(session)
        recipe_ids = sorted({int(event["recipe_id"]) for event in events})
        rows = await self.db().fetch(
            """
            SELECT r.id, r.dish_type, r.cuisine_code, r.cuisine_codes,
                   COALESCE(ing.items, '[]'::jsonb) AS ingredients
            FROM recipe_library.recipes r
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'normalized_name', i.normalized_name,
                    'quantity_min', i.quantity_min,
                    'quantity_max', i.quantity_max
                ) ORDER BY i.position) AS items
                FROM recipe_library.recipe_ingredients i
                WHERE i.recipe_id = r.id
            ) ing ON TRUE
            WHERE r.id = ANY($1::bigint[])
            """,
            recipe_ids,
        )
        recipes = []
        for row in rows:
            recipe = dict(row)
            if isinstance(recipe["ingredients"], str):
                recipe["ingredients"] = json.loads(recipe["ingredients"])
            recipes.append(recipe)
        metas = build_metas(recipes)
        return TasteModel.fit(events, metas, date.today()), metas

    async def taste_summary(self, session: dict[str, Any]) -> dict[str, Any]:
        """Топ любимого и нелюбимого — экран «Вкусы семьи» и бот (§7)."""
        model, metas = await self._taste_model(session)
        summary = model.summary(metas)
        titles = {}
        recipe_ids = [
            item["recipe_id"]
            for group in ("favourite_recipes", "disliked_recipes")
            for item in summary[group]
        ]
        if recipe_ids:
            titles = {
                int(row["id"]): clean_dish_title(row["title"])
                for row in await self.db().fetch(
                    "SELECT id, title FROM recipe_library.recipes WHERE id = ANY($1::bigint[])",
                    recipe_ids,
                )
            }
        for group in ("favourite_recipes", "disliked_recipes"):
            for item in summary[group]:
                item["title"] = titles.get(item["recipe_id"])
        return summary

    async def refresh_taste_affinities(self, session: dict[str, Any]) -> int:
        """Пересчёт аффинити семьи; вызывается ночным джобом и после событий."""
        model, _metas = await self._taste_model(session)
        rows = model.rows()
        async with self.db().acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM app_core.taste_affinities WHERE household_id=$1",
                session["household_id"],
            )
            if rows:
                await connection.executemany(
                    """
                    INSERT INTO app_core.taste_affinities (
                        household_id, level, key, score, events_count
                    ) VALUES ($1,$2,$3,$4,$5)
                    """,
                    [
                        (
                            session["household_id"], row["level"], row["key"],
                            Decimal(str(round(row["score"], 6))), row["events_count"],
                        )
                        for row in rows
                    ],
                )
        return len(rows)

    async def refresh_all_taste_affinities(self) -> int:
        """Ночной пересчёт по всем семьям (§4.2)."""
        households = await self.db().fetch("SELECT id FROM app_core.households")
        total = 0
        for row in households:
            total += await self.refresh_taste_affinities({"household_id": row["id"]})
        return total

    async def taste_onboarding(self, session: dict[str, Any]) -> dict[str, Any]:
        """Карточки холодного старта: по два блюда на частую пару кухня+тип."""
        events = await self.db().fetchval(
            "SELECT count(*) FROM app_core.taste_events WHERE household_id=$1",
            session["household_id"],
        )
        rows = await self.db().fetch(
            """
            WITH pairs AS (
                SELECT r.cuisine_code, r.dish_type, count(*) AS total
                FROM recipe_library.recipes r
                WHERE r.review_status = 'ready'
                  AND r.cuisine_code IS NOT NULL AND r.dish_type IS NOT NULL
                GROUP BY r.cuisine_code, r.dish_type
                ORDER BY total DESC
                LIMIT $2
            )
            SELECT picked.id, picked.title, picked.cuisine_code, picked.dish_type,
                   picked.source_page_start
            FROM pairs
            CROSS JOIN LATERAL (
                SELECT r.id, r.title, r.cuisine_code, r.dish_type, r.source_page_start
                FROM recipe_library.recipes r
                WHERE r.review_status = 'ready'
                  AND r.cuisine_code = pairs.cuisine_code
                  AND r.dish_type = pairs.dish_type
                  AND NOT EXISTS (
                      SELECT 1 FROM app_core.taste_events e
                      WHERE e.household_id = $1 AND e.recipe_id = r.id
                  )
                ORDER BY md5(r.id::text || $1::text)
                LIMIT 2
            ) AS picked
            """,
            session["household_id"], ONBOARDING_PAIRS,
        )
        return {
            "events_count": int(events or 0),
            "needed": int(events or 0) < ONBOARDING_MIN_EVENTS,
            "cards": [
                {
                    "recipe_id": int(row["id"]),
                    "title": clean_dish_title(row["title"]),
                    "cuisine_code": row["cuisine_code"],
                    "dish_type": row["dish_type"],
                    "source_page_start": row["source_page_start"],
                }
                for row in rows
            ],
        }

    async def save_taste_onboarding(
        self, session: dict[str, Any], answers: list[dict[str, Any]]
    ) -> int:
        """Ответы онбординга: 👍 и 👎; пропуск событием не считается."""
        if session["role"] == "viewer":
            raise PermissionError("Режим просмотра не позволяет менять настройки")
        saved = 0
        async with self.db().acquire() as connection, connection.transaction():
            for answer in answers:
                liked = answer.get("liked")
                if liked is None:
                    continue
                await self.record_taste_event(
                    session,
                    int(answer["recipe_id"]),
                    "onboarding_like" if liked else "onboarding_skip",
                    connection=connection,
                )
                saved += 1
        return saved

    async def _rebuild_plan_shopping(
        self, session: dict[str, Any], plan_id: uuid.UUID
    ) -> None:
        """Пересборка списка покупок плана после замены блюда."""
        from .planner import _base_quantity as base_quantity, _normal as normal
        from .planning.candidates import Synonyms
        from .planning.scaling import scaled_quantity
        from .planning.shopping import (
            aggregate_ingredients, build_shopping, prepare_inventory,
        )

        meal_rows = await self.db().fetch(
            "SELECT recipe_id, scale FROM app_core.plan_meals WHERE plan_id=$1",
            plan_id,
        )
        recipe_ids = sorted({int(row["recipe_id"]) for row in meal_rows})
        ingredient_rows = await self.db().fetch(
            """
            SELECT recipe_id, ingredient_text, normalized_name, quantity_min,
                   quantity_max, unit_code, is_to_taste
            FROM recipe_library.recipe_ingredients
            WHERE recipe_id = ANY($1::bigint[])
            ORDER BY recipe_id, position
            """,
            recipe_ids,
        )
        by_recipe: dict[int, list[dict[str, Any]]] = {}
        for row in ingredient_rows:
            by_recipe.setdefault(int(row["recipe_id"]), []).append(dict(row))
        meal_ingredients: list[dict[str, Any]] = []
        for meal in meal_rows:
            scale = Decimal(str(meal["scale"]))
            for ingredient in by_recipe.get(int(meal["recipe_id"]), []):
                meal_ingredients.append(
                    {
                        "name": str(
                            ingredient.get("normalized_name")
                            or ingredient.get("ingredient_text")
                            or "Продукт"
                        ),
                        "quantity": scaled_quantity(ingredient, scale),
                        "unit_code": ingredient.get("unit_code"),
                        "is_to_taste": bool(ingredient.get("is_to_taste")),
                    }
                )
        inventory = [dict(row) for row in await self.db().fetch(
            "SELECT name, quantity, unit_code, expires_on FROM app_core.inventory_lots WHERE household_id=$1 AND quantity>0",
            session["household_id"],
        )]
        synonyms = Synonyms.from_rows(await self.ingredient_synonyms())
        matcher = await self.product_matcher()
        price_tier = await self.db().fetchval(
            "SELECT price_tier FROM app_core.meal_plans WHERE id=$1", plan_id
        )
        aggregate = aggregate_ingredients(meal_ingredients, synonyms, normal, base_quantity)
        lots = prepare_inventory(inventory, synonyms, normal, base_quantity)
        shopping, total_cost, matched_items = build_shopping(
            aggregate, lots, matcher, str(price_tier or "balanced"), base_quantity
        )
        async with self.db().acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM app_core.plan_ingredients WHERE plan_id=$1", plan_id
            )
            for item in shopping:
                await connection.execute(
                    """
                    INSERT INTO app_core.plan_ingredients (
                        id, plan_id, normalized_name, quantity, unit_code,
                        covered_from_inventory, buy_quantity, matched_product_id,
                        pack_count, estimated_cost_kop, to_taste
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    """,
                    uuid.uuid4(), plan_id, item["normalized_name"], item["quantity"],
                    item["unit_code"], item["covered_from_inventory"], item["buy_quantity"],
                    item["matched_product_id"], item["pack_count"], item["estimated_cost_kop"],
                    bool(item.get("to_taste")),
                )
            await connection.execute(
                """
                UPDATE app_core.meal_plans
                SET estimated_cost_kop=$2, matched_cost_items=$3, total_cost_items=$4
                WHERE id=$1
                """,
                plan_id, total_cost, matched_items,
                sum(
                    1 for item in shopping
                    if item["buy_quantity"] is not None and item["buy_quantity"] > 0
                ),
            )

    async def _plan_payload(self, header: asyncpg.Record) -> dict[str, Any]:
        meals = await self.db().fetch(
            """
            SELECT pm.id, pm.meal_date, pm.meal_type, pm.recipe_id, pm.scale,
                   pm.servings, pm.estimated_kcal, pm.warnings, pm.reasons,
                   pm.estimated_protein, pm.estimated_fat, pm.estimated_carb,
                   pm.leftover_of, pm.status,
                   r.title, r.cuisine_code, r.review_status, r.source_page_start
            FROM app_core.plan_meals pm
            JOIN recipe_library.recipes r ON r.id=pm.recipe_id
            WHERE pm.plan_id=$1 ORDER BY pm.meal_date, pm.position
            """,
            header["id"],
        )
        # pi.id обязателен: без него PATCH /api/plans/{id}/items/{item_id}
        # неадресуем. category_slug нужен для группировки списка покупок.
        shopping = await self.db().fetch(
            """
            SELECT pi.id, pi.normalized_name, pi.quantity, pi.unit_code,
                   pi.covered_from_inventory, pi.buy_quantity, pi.matched_product_id,
                   p.name AS matched_product_name, p.url AS matched_product_url,
                   pi.pack_count, pi.estimated_cost_kop,
                   pi.purchased_at, pi.to_taste,
                   (SELECT c.category_slug
                    FROM lenta_store.store_product_categories c
                    WHERE c.product_id = pi.matched_product_id
                    ORDER BY c.category_slug
                    LIMIT 1) AS category_slug
            FROM app_core.plan_ingredients pi
            LEFT JOIN lenta_store.store_products p ON p.id=pi.matched_product_id
            WHERE pi.plan_id=$1 ORDER BY pi.normalized_name
            """,
            header["id"],
        )
        result = row_dict(header)
        result["cuisine_preferences"] = _json_column(result.get("cuisine_preferences"))
        # K4: предупреждения плана и solver_status читаются из БД — раньше тут
        # стояли две статические строки, и budget_exceeded/scale_unknown
        # пропадали после перезагрузки страницы.
        stored_warnings = _json_column(result.pop("plan_warnings", None))
        result["meals"] = [row_dict(row) for row in meals]
        # A1: сохранённый план возвращается всегда. Раньше здесь стоял пост-фильтр
        # по сырому title — из-за него меню молча исчезало после перезагрузки,
        # хотя при генерации проверялось очищенное название. Качество рецептов
        # теперь обеспечивает review_status (TZ-M2R).
        for meal in result["meals"]:
            meal["title"] = clean_dish_title(meal["title"])
            meal["warnings"] = _json_column(meal.get("warnings"))
            meal["reasons"] = _json_column(meal.get("reasons")) or []
        result["shopping"] = [row_dict(row) for row in shopping]
        result["warnings"] = stored_warnings or [
            # Фолбэк для планов, сохранённых до появления plan_warnings.
            "Рецепты импортированы автоматически и пока требуют проверки.",
            "Стоимость рассчитана только для сопоставленных товаров текущего каталога.",
        ]
        return result

    async def telegram_link_token(self, session: dict[str, Any]) -> str:
        raw_token = new_token()
        await self.db().execute(
            """
            INSERT INTO app_core.one_time_tokens (token_hash, user_id, purpose, expires_at)
            VALUES ($1,$2,'telegram_link',$3)
            """,
            token_hash(raw_token), session["user_id"], datetime.now(UTC) + timedelta(minutes=10),
        )
        return raw_token

    async def web_login_code(self, user_id: Any) -> str:
        """Одноразовый код входа в веб для владельца Telegram-аккаунта (§3.3).

        Шесть цифр, а не длинный токен: код придётся набирать руками с телефона.
        Перебор закрыт с трёх сторон — код живёт 5 минут, гасится при первом
        применении, а эндпоинт входа ограничен по частоте.
        """
        expires_at = datetime.now(UTC) + WEB_LOGIN_CODE_TTL
        # Коллизия шестизначного кода маловероятна, но token_hash — первичный
        # ключ, поэтому пробуем несколько раз, а не падаем.
        for _ in range(10):
            code = f"{secrets.randbelow(1_000_000):06d}"
            try:
                await self.db().execute(
                    """
                    INSERT INTO app_core.one_time_tokens (token_hash, user_id, purpose, expires_at)
                    VALUES ($1,$2,'web_login',$3)
                    """,
                    token_hash(code), user_id, expires_at,
                )
            except asyncpg.UniqueViolationError:
                continue
            return code
        raise RuntimeError("Не удалось выдать код входа")

    async def telegram_login(self, code: str) -> tuple[str, str]:
        """Обменять код из бота на сессию веба. Ошибка — та же, что у входа."""
        row = await self.db().fetchrow(
            """
            UPDATE app_core.one_time_tokens
            SET used_at = CURRENT_TIMESTAMP
            WHERE token_hash=$1 AND purpose='web_login'
              AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP
            RETURNING user_id
            """,
            token_hash((code or "").strip()),
        )
        if not row:
            raise AuthenticationError("Код не подошёл: он просрочен или уже использован")
        status = await self.db().fetchval(
            "SELECT status FROM app_core.users WHERE id=$1", row["user_id"]
        )
        if status != "active":
            raise AuthenticationError("Аккаунт недоступен")
        return await self.create_session(row["user_id"])

    async def has_password(self, user_id: Any) -> bool:
        """Есть ли у аккаунта пароль: без него отвязка Telegram запирает вход."""
        return await self.db().fetchval(
            "SELECT 1 FROM app_core.password_credentials WHERE user_id=$1", user_id
        ) is not None

    async def set_password(self, session: dict[str, Any], password: str) -> None:
        """Задать или сменить пароль (§3.3, шаг 4) — для аккаунтов из бота."""
        password_hash = hash_password(password)
        await self.db().execute(
            """
            INSERT INTO app_core.password_credentials (user_id, password_hash)
            VALUES ($1,$2)
            ON CONFLICT (user_id) DO UPDATE
            SET password_hash=EXCLUDED.password_hash, changed_at=CURRENT_TIMESTAMP
            """,
            session["user_id"], password_hash,
        )
        await self.audit(session, "auth.password_set", "user", session["user_id"])

    async def unlink_telegram(self, session: dict[str, Any]) -> bool:
        """Отвязать Telegram (§3.4). Незавершённый диалог тоже стирается —
        это персональные данные, которым незачем переживать отвязку."""
        async with self.db().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                DELETE FROM app_core.auth_identities
                WHERE provider='telegram' AND user_id=$1
                RETURNING provider_user_id
                """,
                session["user_id"],
            )
            if row is not None:
                telegram_id = int(row["provider_user_id"])
                await connection.execute(
                    "DELETE FROM app_core.telegram_dialog_state WHERE user_id=$1",
                    telegram_id,
                )
                # §6: после отвязки напоминания не шлём, и настройки хранить не за чем
                await connection.execute(
                    "DELETE FROM app_core.telegram_notifications WHERE user_id=$1",
                    telegram_id,
                )
                await self._audit(
                    connection, session["household_id"], session["user_id"],
                    "auth.telegram_unlinked", "user", session["user_id"],
                    channel=session.get("channel"),
                )
        return row is not None

    async def audit(
        self,
        session: dict[str, Any],
        action: str,
        entity_type: str | None = None,
        entity_id: Any | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self.db().acquire() as connection:
            await self._audit(
                connection, session["household_id"], session["user_id"], action,
                entity_type, entity_id, details, channel=session.get("channel"),
            )

    def _resolve_channel(self, channel: str | None) -> str:
        """Канал записи: явный → из сессии → умолчание экземпляра репозитория."""
        value = str(channel or self.audit_channel or "web")
        return value if value in AUDIT_CHANNELS else "web"

    async def _audit(
        self,
        connection: asyncpg.Connection,
        household_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        action: str,
        entity_type: str | None = None,
        entity_id: Any | None = None,
        details: dict[str, Any] | None = None,
        *,
        channel: str | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO app_core.audit_log (
                household_id, user_id, channel, action, entity_type, entity_id, details
            ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            """,
            household_id, user_id, self._resolve_channel(channel), action, entity_type,
            str(entity_id) if entity_id is not None else None,
            json.dumps(details or {}, ensure_ascii=False),
        )
