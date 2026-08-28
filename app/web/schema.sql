CREATE SCHEMA IF NOT EXISTS app_core;

CREATE TABLE IF NOT EXISTS app_core.users (
    id UUID PRIMARY KEY,
    login TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('active', 'blocked', 'deleted'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_login_lower ON app_core.users (lower(login));

CREATE TABLE IF NOT EXISTS app_core.password_credentials (
    user_id UUID PRIMARY KEY REFERENCES app_core.users(id) ON DELETE CASCADE,
    password_hash TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_core.user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_core.users(id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_user_sessions_user ON app_core.user_sessions (user_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS app_core.households (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
    created_by UUID NOT NULL REFERENCES app_core.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_core.household_memberships (
    household_id UUID NOT NULL REFERENCES app_core.households(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES app_core.users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (household_id, user_id),
    CHECK (role IN ('owner', 'admin', 'editor', 'viewer'))
);

CREATE TABLE IF NOT EXISTS app_core.people (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES app_core.households(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    person_type TEXT NOT NULL DEFAULT 'adult',
    target_kcal INTEGER,
    portion_factor NUMERIC NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (person_type IN ('adult', 'child')),
    CHECK (target_kcal IS NULL OR target_kcal BETWEEN 500 AND 6000),
    CHECK (portion_factor > 0 AND portion_factor <= 3)
);

CREATE TABLE IF NOT EXISTS app_core.appliances (
    household_id UUID NOT NULL REFERENCES app_core.households(id) ON DELETE CASCADE,
    appliance_code TEXT NOT NULL,
    PRIMARY KEY (household_id, appliance_code)
);

CREATE TABLE IF NOT EXISTS app_core.dietary_rules (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES app_core.households(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL,
    term TEXT NOT NULL,
    is_hard BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (rule_type IN ('allergy', 'intolerance', 'exclude', 'dislike'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dietary_rules
    ON app_core.dietary_rules (household_id, rule_type, lower(term));

CREATE TABLE IF NOT EXISTS app_core.inventory_lots (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES app_core.households(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    unit_code TEXT NOT NULL,
    expires_on DATE,
    storage_area TEXT NOT NULL DEFAULT 'fridge',
    created_by UUID NOT NULL REFERENCES app_core.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (quantity >= 0),
    CHECK (unit_code IN ('g', 'kg', 'ml', 'l', 'piece')),
    CHECK (storage_area IN ('fridge', 'freezer', 'pantry'))
);

CREATE TABLE IF NOT EXISTS app_core.meal_plans (
    id UUID PRIMARY KEY,
    household_id UUID NOT NULL REFERENCES app_core.households(id) ON DELETE CASCADE,
    starts_on DATE NOT NULL,
    days INTEGER NOT NULL,
    budget_kop INTEGER,
    estimated_cost_kop INTEGER,
    matched_cost_items INTEGER NOT NULL DEFAULT 0,
    total_cost_items INTEGER NOT NULL DEFAULT 0,
    price_tier TEXT NOT NULL DEFAULT 'balanced',
    cuisine_preferences JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'draft',
    created_by UUID NOT NULL REFERENCES app_core.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (days BETWEEN 1 AND 7),
    CHECK (price_tier IN ('economy', 'balanced', 'premium')),
    CHECK (status IN ('draft', 'confirmed', 'archived'))
);

ALTER TABLE app_core.meal_plans
    ADD COLUMN IF NOT EXISTS price_tier TEXT NOT NULL DEFAULT 'balanced';

CREATE TABLE IF NOT EXISTS app_core.plan_meals (
    id UUID PRIMARY KEY,
    plan_id UUID NOT NULL REFERENCES app_core.meal_plans(id) ON DELETE CASCADE,
    meal_date DATE NOT NULL,
    meal_type TEXT NOT NULL,
    recipe_id BIGINT NOT NULL REFERENCES recipe_library.recipes(id),
    scale NUMERIC NOT NULL,
    servings NUMERIC NOT NULL,
    estimated_kcal INTEGER,
    position INTEGER NOT NULL DEFAULT 1,
    CHECK (meal_type IN ('breakfast', 'lunch', 'dinner'))
);

-- TZ-M5R §3: предупреждения слота (draft, scale_unknown, …).
ALTER TABLE app_core.plan_meals
    ADD COLUMN IF NOT EXISTS warnings JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS app_core.plan_ingredients (
    id UUID PRIMARY KEY,
    plan_id UUID NOT NULL REFERENCES app_core.meal_plans(id) ON DELETE CASCADE,
    normalized_name TEXT NOT NULL,
    quantity NUMERIC,
    unit_code TEXT,
    covered_from_inventory NUMERIC NOT NULL DEFAULT 0,
    buy_quantity NUMERIC,
    matched_product_id BIGINT REFERENCES lenta_store.store_products(id),
    pack_count INTEGER,
    estimated_cost_kop INTEGER
);

-- Отметка «куплено» в списке покупок (TZ-M6R A4).
ALTER TABLE app_core.plan_ingredients
    ADD COLUMN IF NOT EXISTS purchased_at TIMESTAMPTZ,
    -- «по вкусу» — соль/перец без количества, показываются отдельной пометкой
    ADD COLUMN IF NOT EXISTS to_taste BOOLEAN NOT NULL DEFAULT FALSE;

-- Оценка Б/Ж/У блюда по данным каталога (граммы на приём, приблизительно).
ALTER TABLE app_core.plan_meals
    ADD COLUMN IF NOT EXISTS estimated_protein INTEGER,
    ADD COLUMN IF NOT EXISTS estimated_fat INTEGER,
    ADD COLUMN IF NOT EXISTS estimated_carb INTEGER;

-- Аудит K4: статус солвера и предупреждения плана (budget_exceeded,
-- not_enough_recipes, scale_unknown) должны переживать перезагрузку страницы.
ALTER TABLE app_core.meal_plans
    ADD COLUMN IF NOT EXISTS solver_status TEXT,
    ADD COLUMN IF NOT EXISTS plan_warnings JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Оценка рецепта семьёй (звёзды 1–5).
CREATE TABLE IF NOT EXISTS app_core.recipe_ratings (
    household_id UUID NOT NULL REFERENCES app_core.households(id) ON DELETE CASCADE,
    recipe_id BIGINT NOT NULL REFERENCES recipe_library.recipes(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL,
    updated_by UUID REFERENCES app_core.users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (household_id, recipe_id),
    CHECK (rating BETWEEN 1 AND 5)
);

CREATE INDEX IF NOT EXISTS ix_plan_ingredients_plan
    ON app_core.plan_ingredients (plan_id);

-- TZ-M5R T1: словарь синонимов ингредиентов. kind='form' — словоформа к
-- каноническому продукту (агрегация покупок И ограничения); kind='group' —
-- продукт к аллергенной группе (только ограничения: «мука»→глютен не должна
-- переименовывать муку в списке покупок).
CREATE TABLE IF NOT EXISTS app_core.ingredient_synonyms (
    term TEXT NOT NULL,
    canonical TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'form',
    PRIMARY KEY (term, kind),
    CHECK (kind IN ('form', 'group'))
);

INSERT INTO app_core.ingredient_synonyms (term, canonical, kind) VALUES
    ('молока', 'молоко', 'form'),
    ('молоку', 'молоко', 'form'),
    ('молоком', 'молоко', 'form'),
    ('молочный', 'молоко', 'form'),
    ('молочная', 'молоко', 'form'),
    ('молочное', 'молоко', 'form'),
    ('яйца', 'яйцо', 'form'),
    ('яиц', 'яйцо', 'form'),
    ('яйцом', 'яйцо', 'form'),
    ('яичный', 'яйцо', 'form'),
    ('яичная', 'яйцо', 'form'),
    ('орехи', 'орех', 'form'),
    ('орехов', 'орех', 'form'),
    ('ореховый', 'орех', 'form'),
    ('грибы', 'гриб', 'form'),
    ('грибов', 'гриб', 'form'),
    ('грибной', 'гриб', 'form'),
    ('грибная', 'гриб', 'form'),
    ('томаты', 'томат', 'form'),
    ('томатов', 'томат', 'form'),
    ('помидор', 'томат', 'form'),
    ('помидоры', 'томат', 'form'),
    ('помидоров', 'томат', 'form'),
    ('сливки', 'сливки', 'form'),
    ('сливок', 'сливки', 'form'),
    ('сливками', 'сливки', 'form'),
    ('сливочное', 'сливочный', 'form'),
    ('луковица', 'лук', 'form'),
    ('луковицы', 'лук', 'form'),
    ('луковиц', 'лук', 'form'),
    ('чеснока', 'чеснок', 'form'),
    ('чесноком', 'чеснок', 'form'),
    ('моркови', 'морковь', 'form'),
    ('морковка', 'морковь', 'form'),
    ('морковки', 'морковь', 'form'),
    ('картошка', 'картофель', 'form'),
    ('картошки', 'картофель', 'form'),
    ('картофеля', 'картофель', 'form'),
    ('рыбы', 'рыба', 'form'),
    ('рыбный', 'рыба', 'form'),
    ('рыбное', 'рыба', 'form'),
    ('креветками', 'креветки', 'form'),
    ('креветок', 'креветки', 'form'),
    ('мёд', 'мед', 'form'),
    ('мёда', 'мед', 'form'),
    ('меда', 'мед', 'form'),
    ('сахара', 'сахар', 'form'),
    ('сахаром', 'сахар', 'form'),
    ('арахиса', 'арахис', 'form'),
    ('фундука', 'фундук', 'form'),
    ('миндаля', 'миндаль', 'form'),
    -- Аллергенные группы (только для ограничений):
    ('фундук', 'орех', 'group'),
    ('миндаль', 'орех', 'group'),
    ('кешью', 'орех', 'group'),
    ('фисташки', 'орех', 'group'),
    ('фисташка', 'орех', 'group'),
    ('арахис', 'орех', 'group'),
    ('пекан', 'орех', 'group'),
    ('пшеница', 'глютен', 'group'),
    ('пшеничная', 'глютен', 'group'),
    ('пшеничный', 'глютен', 'group'),
    ('мука', 'глютен', 'group'),
    ('рожь', 'глютен', 'group'),
    ('ржаная', 'глютен', 'group'),
    ('ячмень', 'глютен', 'group'),
    ('булгур', 'глютен', 'group'),
    ('манка', 'глютен', 'group'),
    ('манная', 'глютен', 'group'),
    ('кускус', 'глютен', 'group'),
    ('молоко', 'лактоза', 'group'),
    ('сливки', 'лактоза', 'group'),
    ('сметана', 'лактоза', 'group'),
    ('кефир', 'лактоза', 'group'),
    ('йогурт', 'лактоза', 'group'),
    ('творог', 'лактоза', 'group'),
    ('сыр', 'лактоза', 'group'),
    ('лосось', 'рыба', 'group'),
    ('семга', 'рыба', 'group'),
    ('треска', 'рыба', 'group'),
    ('тунец', 'рыба', 'group'),
    ('скумбрия', 'рыба', 'group'),
    ('сельдь', 'рыба', 'group'),
    ('форель', 'рыба', 'group'),
    ('креветки', 'морепродукты', 'group'),
    ('кальмар', 'морепродукты', 'group'),
    ('кальмары', 'морепродукты', 'group'),
    ('мидии', 'морепродукты', 'group'),
    ('гребешки', 'морепродукты', 'group'),
    ('яйцо', 'яйцо', 'group'),
    ('мед', 'мед', 'group')
ON CONFLICT (term, kind) DO NOTHING;

CREATE TABLE IF NOT EXISTS app_core.auth_identities (
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES app_core.users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, provider_user_id),
    UNIQUE (provider, user_id)
);

CREATE TABLE IF NOT EXISTS app_core.one_time_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_core.users(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_core.audit_log (
    id BIGSERIAL PRIMARY KEY,
    household_id UUID REFERENCES app_core.households(id) ON DELETE SET NULL,
    user_id UUID REFERENCES app_core.users(id) ON DELETE SET NULL,
    channel TEXT NOT NULL DEFAULT 'web',
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_people_household ON app_core.people (household_id, position);
CREATE INDEX IF NOT EXISTS ix_inventory_household_expiry ON app_core.inventory_lots (household_id, expires_on);
CREATE INDEX IF NOT EXISTS ix_meal_plans_household_created ON app_core.meal_plans (household_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_plan_meals_plan_date ON app_core.plan_meals (plan_id, meal_date, meal_type);
CREATE INDEX IF NOT EXISTS ix_audit_household_created ON app_core.audit_log (household_id, created_at DESC);
