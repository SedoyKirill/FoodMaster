"""Схемы входных данных и допустимые значения — общие для веба и бота.

TZ-M7 §2: бот не ходит по HTTP в веб, но проверяет введённое теми же
правилами, что и форма в браузере, и теми же словами объясняет ошибку.
Раньше модели жили в ``main.py`` вперемешку с маршрутами, и второй канал
неизбежно завёл бы свои, слегка другие.

Здесь только форма данных. Проверки, которые зависят от состояния (роль,
сегодняшняя дата, наличие семьи), остаются в обработчиках: у них другой код
ответа и другие формулировки.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

#: Единицы измерения запасов (``inventory_lots.unit_code`` в схеме).
UNIT_CODES = ("g", "kg", "ml", "l", "piece")
#: Где лежит продукт.
STORAGE_AREAS = ("fridge", "freezer", "pantry")
#: Стратегия подбора цен в плане. TZ-M8 заменит её режимами планирования.
PRICE_TIERS = ("economy", "balanced", "premium")

#: Тексты ошибок: одни и те же в форме браузера и в чате.
UNKNOWN_UNIT_TEXT = "Неизвестная единица"
UNKNOWN_STORAGE_TEXT = "Неизвестное место хранения"
EXPIRED_TEXT = "Срок годности в прошлом. Отметьте «уже просрочено», если это верно."
UNKNOWN_TIER_TEXT = "Неизвестная ценовая стратегия"
HOUSEHOLD_NAME_TEXT = "Название семьи: от 1 до 100 символов"

#: Границы названия семьи — те же, что у SettingsPayload.household_name.
HOUSEHOLD_NAME_MIN = 1
HOUSEHOLD_NAME_MAX = 100


def validate_household_name(value: str) -> str:
    """Название семьи из свободного ввода (бот спрашивает его текстом)."""
    value = (value or "").strip()
    if not HOUSEHOLD_NAME_MIN <= len(value) <= HOUSEHOLD_NAME_MAX:
        raise ValueError(HOUSEHOLD_NAME_TEXT)
    return value


class AuthPayload(BaseModel):
    login: str
    password: str
    household_name: str = "Моя семья"


class TelegramLoginPayload(BaseModel):
    """Вход в веб по одноразовому коду из бота (TZ-M7 §3.3)."""

    code: str = Field(min_length=1, max_length=64)


class PasswordPayload(BaseModel):
    """Пароль для аккаунта, заведённого из бота (TZ-M7 §3.3, шаг 4)."""

    password: str


class PersonPayload(BaseModel):
    id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=80)
    person_type: str = "adult"
    target_kcal: int | None = Field(default=None, ge=500, le=6000)
    portion_factor: Decimal = Field(default=Decimal("1"), gt=0, le=3)


class RulePayload(BaseModel):
    rule_type: str = "exclude"
    term: str = Field(min_length=1, max_length=100)
    is_hard: bool = True


class SettingsPayload(BaseModel):
    household_name: str = Field(min_length=HOUSEHOLD_NAME_MIN, max_length=HOUSEHOLD_NAME_MAX)
    people: list[PersonPayload]
    appliances: list[str] = Field(default_factory=list)
    dietary_rules: list[RulePayload] = Field(default_factory=list)


class InventoryPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    quantity: Decimal = Field(gt=0, le=1_000_000)
    unit_code: str
    expires_on: date | None = None
    storage_area: str = "fridge"
    # Срок в прошлом ломает сортировку FEFO, поэтому его надо подтвердить явно (S8).
    already_expired: bool = False


class ReviewPayload(BaseModel):
    status: str


class PurchasePayload(BaseModel):
    purchased: bool = True


class PersonPatchPayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    person_type: str | None = None
    target_kcal: int | None = Field(default=None, ge=500, le=6000)
    portion_factor: Decimal | None = Field(default=None, gt=0, le=3)


class PlanPayload(BaseModel):
    starts_on: date = Field(default_factory=date.today)
    days: int = Field(default=3, ge=1, le=7)
    cuisines: list[str] = Field(default_factory=list)
    budget_rub: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    price_tier: str = "balanced"


class RatingPayload(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)


class ReplaceMealPayload(BaseModel):
    recipe_id: int | None = Field(default=None, ge=1)
