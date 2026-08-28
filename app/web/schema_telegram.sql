-- =====================================================================
-- TZ-M7 §7 — объекты Telegram-бота.
--
-- Отдельный файл, а не хвост schema.sql: над schema.sql параллельно идёт
-- работа по TZ-M8, и две сессии, дописывающие в один и тот же конец файла,
-- гарантированно мешают друг другу.
--
-- Всё идемпотентно и применяется на каждом старте web
-- (AppRepository.connect → apply_schema). Зависит от app/web/schema.sql
-- (users, households, household_memberships), поэтому идёт строго после него.
-- =====================================================================

-- --- §3.1: активная семья --------------------------------------------
-- NULL означает «первое членство по created_at» — прежнее поведение.
ALTER TABLE app_core.users
    ADD COLUMN IF NOT EXISTS active_household_id UUID
        REFERENCES app_core.households(id) ON DELETE SET NULL;

-- Бэкофилл первым членством. Повторный прогон ничего не делает и не
-- перетирает выбор, сделанный пользователем.
UPDATE app_core.users u
SET active_household_id = m.household_id
FROM (
    SELECT DISTINCT ON (user_id) user_id, household_id
    FROM app_core.household_memberships
    ORDER BY user_id, created_at, household_id
) m
WHERE m.user_id = u.id
  AND u.active_household_id IS NULL;

-- --- §3.1: привязка по Telegram from.id, а не по chat_id --------------
-- from.id пользователя всегда положительный, chat.id группы — отрицательный.
-- В личном чате они совпадают, поэтому старые положительные строки верны.
-- Групповые привязки давали доступ к семье всем участникам чата (А2 их
-- запрещает): помечаем их как устаревшие, чтобы владелец мог перепривязаться,
-- но данные о привязке не терялись молча.
UPDATE app_core.auth_identities
SET provider = 'telegram_stale'
WHERE provider = 'telegram'
  AND provider_user_id !~ '^[1-9][0-9]*$';

-- Дальше такие строки не появляются. DROP + ADD — идемпотентная замена
-- отсутствующего в PostgreSQL «ADD CONSTRAINT IF NOT EXISTS»; таблица
-- крошечная, блокировка на миллисекунды.
ALTER TABLE app_core.auth_identities
    DROP CONSTRAINT IF EXISTS ck_auth_identities_telegram_user;
ALTER TABLE app_core.auth_identities
    ADD CONSTRAINT ck_auth_identities_telegram_user
    CHECK (provider <> 'telegram' OR provider_user_id ~ '^[1-9][0-9]*$');

-- --- §7 / T3: состояние диалога --------------------------------------
-- Ключ — Telegram from.id, а не users.id: сцена регистрации (§3.2) начинает
-- диалог до того, как аккаунт создан. Отсюда же BIGINT и отсутствие FK.
-- В data живут только поля текущей формы: тексты сообщений не храним (TZ-M1).
CREATE TABLE IF NOT EXISTS app_core.telegram_dialog_state (
    user_id BIGINT PRIMARY KEY,
    scene TEXT NOT NULL,
    step TEXT NOT NULL DEFAULT '',
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_telegram_dialog_updated
    ON app_core.telegram_dialog_state (updated_at);

-- --- §6: напоминания -------------------------------------------------------
-- Ключ снова Telegram from.id: письма уходят в чат, а не в аккаунт, и по этому
-- же идентификатору бот ищет, кому слать. Строка появляется, только когда
-- человек трогает тумблер: пока её нет, действуют умолчания из кода
-- (утреннее меню и сроки годности включены, остальное — по желанию).
-- last_sent_on даёт дедупликацию: цикл крутится раз в минуту, а уведомление
-- должно уйти один раз в день, даже если бот перезапускали.
CREATE TABLE IF NOT EXISTS app_core.telegram_notifications (
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    hour SMALLINT NOT NULL DEFAULT 8,
    last_sent_on DATE,
    PRIMARY KEY (user_id, kind),
    CHECK (hour BETWEEN 0 AND 23)
);
