"""Собственные запросы бота: привязка личности и лёгкие выборки для сообщений.

Всё остальное бот берёт из того же ``AppRepository``, что и веб (TZ-M7 §2):
здесь только то, чего в вебе нет, — сопоставление Telegram-аккаунта с семьёй.
"""

from __future__ import annotations

from typing import Any

from app.web.security import token_hash


def bot_session(context: dict[str, Any]) -> dict[str, Any]:
    """Псевдосессия бота для ``AppRepository``.

    Те же ключи, что отдаёт ``authenticate`` вебу, плюс канал: с ним
    ``audit_log`` перестаёт помечать действия бота как 'web' (TZ-M7 §3.5).
    """
    return {
        "household_id": context["household_id"],
        "user_id": context["user_id"],
        "role": context["role"],
        "login": context.get("login"),
        "household_name": context.get("household_name"),
        "channel": "telegram",
    }


class BotRepository:
    """Привязка Telegram-аккаунта к семье и выборки для «Сегодня»/«Покупки»."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def link_user(self, user_id: int, raw_token: str) -> str | None:
        """Погасить одноразовый токен и привязать Telegram-аккаунт.

        ``user_id`` — Telegram ``from.id`` (TZ-M7 §3.1), а не id чата: иначе в
        групповом чате доступ к семье получали все участники.
        Возвращает login привязанного аккаунта или None.
        """
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE app_core.one_time_tokens
                SET used_at = CURRENT_TIMESTAMP
                WHERE token_hash=$1 AND purpose='telegram_link'
                  AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                RETURNING user_id
                """,
                token_hash(raw_token),
            )
            if not row:
                return None
            account_id = row["user_id"]
            await connection.execute(
                """
                DELETE FROM app_core.auth_identities
                WHERE provider='telegram' AND (provider_user_id=$1 OR user_id=$2)
                """,
                str(user_id), account_id,
            )
            await connection.execute(
                """
                INSERT INTO app_core.auth_identities (provider, provider_user_id, user_id)
                VALUES ('telegram', $1, $2)
                """,
                str(user_id), account_id,
            )
            login = await connection.fetchval(
                "SELECT login FROM app_core.users WHERE id=$1", account_id
            )
            return str(login or "")

    async def context_for_user(self, user_id: int) -> dict[str, Any] | None:
        """Аккаунт, активная семья и роль по Telegram ``from.id``.

        None — аккаунт не привязан. Активная семья — ``users.active_household_id``,
        если членство в ней ещё живо; иначе первое членство по ``created_at``
        (прежнее поведение). Порядком, а не COALESCE: при «протухшем»
        active_household_id COALESCE вернул бы пусто и бот молча замолчал бы.
        """
        row = await self.pool.fetchrow(
            """
            SELECT u.id AS user_id, u.login,
                   h.id AS household_id, h.name AS household_name,
                   h.timezone, m.role
            FROM app_core.auth_identities ai
            JOIN app_core.users u ON u.id = ai.user_id AND u.status='active'
            JOIN LATERAL (
                SELECT hm.household_id, hm.role
                FROM app_core.household_memberships hm
                WHERE hm.user_id = u.id
                ORDER BY (hm.household_id = u.active_household_id) DESC NULLS LAST,
                         hm.created_at
                LIMIT 1
            ) m ON TRUE
            JOIN app_core.households h ON h.id = m.household_id
            WHERE ai.provider='telegram' AND ai.provider_user_id=$1
            """,
            str(user_id),
        )
        return dict(row) if row else None

    async def latest_plan_meals(self, household_id: Any) -> list[dict[str, Any]]:
        """Блюда последнего плана семьи (весь горизонт), с id для кнопок."""
        rows = await self.pool.fetch(
            """
            SELECT pm.id, pm.plan_id, pm.meal_date, pm.meal_type, pm.position,
                   pm.recipe_id, pm.estimated_kcal, pm.estimated_protein,
                   pm.estimated_fat, pm.estimated_carb, r.title
            FROM app_core.plan_meals pm
            JOIN recipe_library.recipes r ON r.id = pm.recipe_id
            WHERE pm.plan_id = (
                SELECT id FROM app_core.meal_plans
                WHERE household_id=$1 ORDER BY created_at DESC LIMIT 1
            )
            ORDER BY pm.meal_date, pm.position
            """,
            household_id,
        )
        return [dict(row) for row in rows]

    async def shopping_items(self, household_id: Any) -> list[dict[str, Any]]:
        """Список покупок последнего плана семьи, с id для кнопок."""
        rows = await self.pool.fetch(
            """
            SELECT pi.id, pi.plan_id, pi.normalized_name, pi.buy_quantity,
                   pi.unit_code, pi.pack_count, pi.estimated_cost_kop,
                   pi.purchased_at, pi.to_taste
            FROM app_core.plan_ingredients pi
            WHERE pi.plan_id = (
                SELECT id FROM app_core.meal_plans
                WHERE household_id=$1 ORDER BY created_at DESC LIMIT 1
            )
            ORDER BY pi.normalized_name
            """,
            household_id,
        )
        return [dict(row) for row in rows]
